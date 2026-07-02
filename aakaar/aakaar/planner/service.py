"""Planner orchestrator.

One call to `plan(...)`:
  1. Build the system prompt with the tenant's grants.
  2. Send the conversation to the LLM, requesting a `PlannerCompletion`.
  3. If the response is `dag`, validate against the registry + grants. On
     failure, feed errors back and retry up to N times.
  4. Return a `PlannerResponse` (the public, three-way union).

The planner is stateless. Persistence (chat history, saved DAGs, run logs)
is the caller's job.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from aakaar.planner.llm import LLMClient, LLMMessage, PlannerCompletion
from aakaar.planner.prompt import PromptBuilder
from aakaar.shared.dag import (
    UNGRANTED_MARKER,
    ValidationError,
    auto_complete_edges,
    explain_dag_errors,
    validate_dag_collect,
)
from aakaar.shared.dag.types import Dag
from aakaar.shared.planner.responses import (
    ClarifyResponse,
    DagResponse,
    MissingResponse,
    PlannerResponse,
)
from aakaar.shared.registry import Registry

logger = logging.getLogger(__name__)

_UNGRANTED_CAP_RE = re.compile(r"uses capability '([^']+)' which is not granted")


class PlannerError(RuntimeError):
    """Raised when the planner cannot produce a valid response within the
    repair budget. The chat UI should surface the error verbatim."""


@dataclass(slots=True)
class PlannerService:
    """Orchestrates one turn of NL→PlannerResponse for a tenant.

    `max_repair_attempts` bounds how many times we feed validation errors
    back to the LLM before giving up. 2 is usually enough — if the model
    can't fix it in two tries, it usually can't fix it at all.
    """

    registry: Registry
    llm: LLMClient
    max_repair_attempts: int = 2

    def plan(
        self,
        *,
        user_message: str,
        granted_capabilities: set[str],
        granted_aliases: dict[str, list[str]] | None = None,
        grant_input_defaults: dict[str, dict[str, dict[str, Any]]] | None = None,
        current_dag: Dag | None = None,
        chat_history: list[LLMMessage] | None = None,
    ) -> PlannerResponse:
        builder = PromptBuilder(
            registry=self.registry,
            granted_capabilities=granted_capabilities,
            granted_aliases=granted_aliases or {},
            grant_input_defaults=grant_input_defaults or {},
        )

        logger.debug(
            "planner.plan: granted_caps=%d aliases=%d has_current_dag=%s history=%d",
            len(granted_capabilities),
            len(granted_aliases or {}),
            current_dag is not None,
            len(chat_history or []),
        )
        repair_errors: list[str] = []
        for attempt in range(self.max_repair_attempts + 1):
            messages = builder.build_messages(
                user_message=user_message,
                current_dag=current_dag,
                chat_history=chat_history,
                repair_errors=repair_errors or None,
            )
            logger.debug("planner LLM call attempt=%d messages=%d", attempt, len(messages))

            # --- LLM completion stage ------------------------------------
            # A malformed completion (bad JSON envelope, or an invalid node ref
            # that fails the schema's ref pattern) surfaces here as a
            # ValidationError from the client. Treat it as REPAIRABLE — feed the
            # error back so the model fixes it, exactly like a DAG validation
            # failure — instead of 502-ing on the first bad output.
            try:
                completion = self.llm.complete_planner(messages)
            except ValidationError as e:
                errs = _split_errors(str(e))
                logger.info(
                    "planner attempt %d produced a malformed completion (%d issue(s)): %s",
                    attempt, len(errs), "; ".join(errs),
                )
                if attempt == self.max_repair_attempts:
                    raise PlannerError(
                        f"planner could not produce a valid response after "
                        f"{attempt + 1} attempts: {'; '.join(errs)}"
                    ) from e
                repair_errors = errs
                continue

            logger.debug("planner LLM completion kind=%s attempt=%d", completion.kind, attempt)

            # --- convert + short-circuit --------------------------------
            # `_convert` may raise `_UngrantedCapabilities` when the ONLY
            # problem is ungranted caps — turn that into an honest `missing`
            # result instead of burning repair attempts on something the LLM
            # can't fix by editing the DAG.
            try:
                resp = self._convert(
                    completion=completion,
                    granted_capabilities=granted_capabilities,
                )
                logger.info(
                    "planner ok kind=%s attempt=%d", completion.kind, attempt
                )
                return resp
            except _UngrantedCapabilities as gap:
                logger.info(
                    "planner short-circuit: %d ungranted cap(s) -> missing: %s",
                    len(gap.needed), ", ".join(gap.needed),
                )
                return MissingResponse(needed=gap.needed, explanation=gap.explanation)
            except ValidationError as e:
                # `e` carries every validation error (collect-all). Feed them
                # ALL back so the model fixes every problem in one repair round
                # instead of one per round.
                errs = _split_errors(str(e))
                logger.info(
                    "planner attempt %d failed validation (%d error(s)): %s",
                    attempt, len(errs), "; ".join(errs),
                )
                if attempt == self.max_repair_attempts:
                    logger.warning(
                        "planner gave up after %d attempts; final errors: %s",
                        attempt + 1,
                        "; ".join(errs),
                    )
                    raise PlannerError(
                        f"planner could not produce a valid DAG after "
                        f"{attempt + 1} attempts: {'; '.join(errs)}"
                    ) from e
                repair_errors = errs

        # Unreachable; the loop either returns or raises.
        raise PlannerError("planner exited without producing a response")

    # --- internals ---------------------------------------------------------

    def _convert(
        self,
        *,
        completion: PlannerCompletion,
        granted_capabilities: set[str],
    ) -> PlannerResponse:
        if completion.kind == "clarify":
            return ClarifyResponse(questions=list(completion.questions))
        if completion.kind == "missing":
            return MissingResponse(
                needed=list(completion.needed),
                explanation=completion.explanation,
            )
        # kind == "dag" — auto-complete missing data-flow edges, then
        # validate. The LLM regularly forgets to mirror `${A.x}`
        # references in the `edges` list; auto_complete_edges turns
        # that recurring bug into a no-op.
        assert completion.dag is not None  # invariant enforced by PlannerCompletion
        completed = auto_complete_edges(completion.dag)
        errors = validate_dag_collect(
            completed,
            registry=self.registry,
            granted_capabilities=granted_capabilities,
        )
        if not errors:
            return DagResponse(dag=completed, rationale=completion.rationale)

        # If EVERY problem is an ungranted capability, the LLM can't repair it
        # by editing the DAG — those capabilities simply aren't available. Turn
        # it into a `missing` result naming the caps rather than looping.
        ungranted = _ungranted_from_errors(errors)
        if ungranted is not None and len(ungranted) == len(errors):
            needed = sorted({m.group(1) for e in errors if (m := _UNGRANTED_CAP_RE.search(e))})
            raise _UngrantedCapabilities(
                needed=needed,
                explanation=(
                    "This request needs "
                    + ("capability " if len(needed) == 1 else "capabilities ")
                    + ", ".join(needed)
                    + ", which "
                    + ("is" if len(needed) == 1 else "are")
                    + " not granted to your tenant. Ask a tenant admin to add "
                    + ("this grant" if len(needed) == 1 else "these grants")
                    + " via the admin grants UI, then try again."
                ),
            )

        # Otherwise feed enriched hints back so the model converges faster.
        hints = explain_dag_errors(
            errors,
            known_refs=self._known_refs(),
            known_aliases=self._aliases_in(completed),
            sample_inputs=self._sample_inputs_for(completed),
        )
        raise ValidationError("\n".join(hints))

    def _known_refs(self) -> list[str]:
        return sorted(d.ref for d in self.registry)

    @staticmethod
    def _aliases_in(dag: Dag) -> list[str]:
        aliases: set[str] = set()
        for n in dag.nodes:
            aliases.add(n.id)
            if n.outputs_as is not None:
                aliases.add(n.outputs_as)
        return sorted(aliases)

    def _sample_inputs_for(self, dag: Dag) -> dict[str, str]:
        """Best-effort field-name → JSON sample for required inputs, pulled from
        each node's declared schema — used to enrich missing-input hints. Never
        invents values; only maps a field name to a type-appropriate sample."""
        samples: dict[str, str] = {}
        for node in dag.nodes:
            defn = self.registry.get(node.ref)
            if defn is None:
                continue
            for fname, finfo in defn.input_schema.model_fields.items():
                if fname in samples:
                    continue
                samples[fname] = _sample_json(fname, finfo)
        return samples


class _UngrantedCapabilities(Exception):
    """Internal signal: the DAG's only problem is ungranted capabilities.

    Carries the needed refs + a user-facing explanation so `plan` can return a
    `MissingResponse` instead of looping through repair attempts."""

    def __init__(self, *, needed: list[str], explanation: str) -> None:
        super().__init__(explanation)
        self.needed = needed
        self.explanation = explanation


def _split_errors(message: str) -> list[str]:
    return [line for line in message.split("\n") if line.strip()]


def _ungranted_from_errors(errors: list[str]) -> list[str] | None:
    """Return the subset of ungranted-capability errors, or None if there are
    none (so callers can tell "all ungranted" from "no ungranted")."""
    ung = [e for e in errors if UNGRANTED_MARKER in e]
    return ung or None


def _sample_json(name: str, info: Any) -> str:
    """Paste-ready JSON sample for one pydantic field, honouring its type then a
    name-based heuristic. Mirrors the executor's expected literal shapes."""
    import json as _json
    import typing as _t

    ann = getattr(info, "annotation", None)
    origin = _t.get_origin(ann)
    args = _t.get_args(ann)
    if origin in (_t.Union, getattr(__import__("types"), "UnionType", ())):
        non_none = [a for a in args if a is not type(None)]
        if non_none:
            ann = non_none[0]
            origin = _t.get_origin(ann)
    if origin is _t.Literal and args:
        return _json.dumps(args[0])
    if ann is bool:
        return "false"
    if ann is int:
        return "0"
    if ann is float:
        return "0.0"
    if origin is list:
        return "[]"
    if origin is dict:
        return "{}"
    return _json.dumps(_str_sample(name))


def _str_sample(name: str) -> str:
    n = name.lower()
    table = (
        ("url", "https://example.com"),
        ("uri", "aakaar://objects/<id>"),
        ("selector", "#submit"),
        ("label", "Submit"),
        ("email", "user@example.com"),
        ("date", "2026-06-04"),
        ("alias", "primary"),
        ("path", "/path/to/file.csv"),
        ("hint", "the report I need"),
        ("text", "Submit"),
        ("message", "Please confirm"),
    )
    for needle, sample in table:
        if needle in n:
            return sample
    return "example"
