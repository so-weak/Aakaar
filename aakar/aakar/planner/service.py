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
from dataclasses import dataclass

from aakar.planner.llm import LLMClient, LLMMessage, PlannerCompletion
from aakar.planner.prompt import PromptBuilder
from aakar.shared.dag import ValidationError, validate_dag
from aakar.shared.dag.types import Dag
from aakar.shared.planner.responses import (
    ClarifyResponse,
    DagResponse,
    MissingResponse,
    PlannerResponse,
)
from aakar.shared.registry import Registry


logger = logging.getLogger(__name__)


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
        current_dag: Dag | None = None,
        chat_history: list[LLMMessage] | None = None,
    ) -> PlannerResponse:
        builder = PromptBuilder(
            registry=self.registry, granted_capabilities=granted_capabilities
        )

        repair_errors: list[str] = []
        for attempt in range(self.max_repair_attempts + 1):
            messages = builder.build_messages(
                user_message=user_message,
                current_dag=current_dag,
                chat_history=chat_history,
                repair_errors=repair_errors or None,
            )
            completion = self.llm.complete_planner(messages)
            try:
                return self._convert(
                    completion=completion,
                    granted_capabilities=granted_capabilities,
                )
            except ValidationError as e:
                logger.info("planner attempt %d failed validation: %s", attempt, e)
                if attempt == self.max_repair_attempts:
                    raise PlannerError(
                        f"planner could not produce a valid DAG after {attempt + 1} attempts: {e}"
                    ) from e
                repair_errors = [str(e)]

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
        # kind == "dag" — validate before returning.
        assert completion.dag is not None  # invariant enforced by PlannerCompletion
        validate_dag(
            completion.dag,
            registry=self.registry,
            granted_capabilities=granted_capabilities,
        )
        return DagResponse(dag=completion.dag, rationale=completion.rationale)
