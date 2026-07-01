"""Agentic planner — the LLM gets browser tools and explores the page.

Used as a fallback when the one-shot `PlannerService` returned a clarify
that's just asking for selectors / URLs / things a browser could
discover. The agentic loop:

  1. Build the system prompt with the registry + grants + tool inventory.
  2. Call the LLM with tools enabled.
  3. If the LLM returned a tool call, run it and append the result to the
     conversation. Repeat.
  4. If the LLM called `done(...)`, parse + validate the DAG and return.
  5. If we hit the iteration cap, return a clarify response listing what
     was learned so the user can fill the gap.

Budget defaults are conservative (12 tool calls / 60 seconds) — enough
for a typical login → dashboard → reports flow without runaway cost.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from aakaar.planner.agentic.runner import PlannerToolRunner
from aakaar.planner.agentic.tools import (
    all_tool_schemas,
    dispatch,
)
from aakaar.planner.llm import LLMClient, LLMMessage, Role, ToolCall
from aakaar.planner.prompt import PromptBuilder
from aakaar.shared.dag import ValidationError, auto_complete_edges, validate_dag
from aakaar.shared.dag.types import Dag
from aakaar.shared.planner.responses import (
    ClarifyResponse,
    DagResponse,
    MissingResponse,
    PlannerResponse,
)
from aakaar.shared.registry import Registry
from aakaar.vault import Vault

logger = logging.getLogger(__name__)


_AGENTIC_SYSTEM_NOTE = """\
# Agentic mode

You have browser tools (navigate, inspect_page, click, login_with_grant)
plus `done(...)` to finalize. Use them to figure out what the workflow
should do, then call `done` ONCE with a complete plan.

Rules:
- Inspect a page BEFORE picking selectors for it. The selectors you
  emit in the final DAG must match what the page actually has.
- When the request depends on what appears AFTER a click ("after clicking
  Continue, fill the date"), use the `click` tool to click and re-inspect
  in one step, then read the post-click fields — don't ask the user what
  they're called. Only click NON-destructive controls at plan time (OK,
  Cancel, Continue, Next, Back, Show, View, tabs, accordions); never click
  Submit, Pay, Send, Delete, Confirm, or Logout while planning — those
  belong in the final DAG at execute time.
- If the user mentions a specific page ("recon upload page",
  "settings page"), navigate to that page AND immediately call
  `inspect_page` on it before emitting selectors. Each navigate
  MUST be followed by an inspect_page if you intend to interact
  with that page. Inspecting only the post-login dashboard is NOT
  enough — the form selectors live on the form's own page.
- For multi-field FORM filling, prefer `browser.set_field(label,
  value)` over `browser.fill` / `browser.select` / `browser.click`.
  set_field locates the control by label text and dispatches by
  control type (select, input, radio, date) — you don't need a CSS
  selector at all. Example: to set "Switch Type" to "Issuer", emit
  `browser.set_field(session, label="Switch Type", value="Issuer")`.
- For navigation between pages of the same site, prefer
  `browser.click_by_text(text="Recon Upload")` over
  `browser.navigate(url="...")` — the URL path may not be obvious
  (e.g. /recon/upload vs /recon-upload), but the visible nav-link
  text is.
- For logout, prefer `browser.click_by_text(text="Logout")` over
  guessing a CSS selector. Same for any other clickable item whose
  selector you didn't directly confirm via inspect_page.
- Use selectors from `inspect_page` results VERBATIM only when you
  inspected that exact page. Do NOT invent `#id` or `[name=...]`
  selectors that aren't in the inspect output.
- If the user mentioned a specific element ("first report", "Biller
  Transactions"), find it by inspecting the relevant page; don't guess.
- Login via `login_with_grant`: credentials come from the vault, never
  from chat. Default `account_alias` to "primary" if the user didn't
  say. If a captcha is detected, do NOT try to log in at plan time —
  emit a DAG that uses `cap.web_login` (which handles captchas at run
  time via HITL).
- For "download X" where X is a recognizable name on the post-login
  page, prefer `cap.file_download(target_hint="X")`. Its runtime
  discovery handles fuzzy matching and HITL ambiguity, so you don't
  need to inspect just to find a download link. Inspect when the page
  needs multi-field form filling (selects, radios, dates, file inputs)
  — that's where exact selectors matter.
- When the user references "today" or any relative date, emit a
  `time.now` node and reference its output (e.g.
  `${now.ist_date}`) instead of baking a literal date into the DAG.
- When the user says "upload /path/to/file", emit a `file.read_local`
  node first (returns a managed-storage URI), then pass that URI to
  `cap.file_upload`. Do NOT try to read the local path yourself.
- When you have enough information, call `done` with kind="dag" and the
  full DAG. If you genuinely cannot figure out what to do (e.g., the
  user wants something the registry can't express), call `done` with
  kind="clarify" or kind="missing" instead.
- Don't loop forever. After ~8 inspect/navigate calls you should know
  enough to emit a DAG."""


@dataclass(slots=True)
class AgenticPlannerService:
    """Tool-driven plan loop.

    Mirrors PlannerService.plan() but produces the same `PlannerResponse`
    via a multi-turn LLM conversation with browser tools.
    """

    registry: Registry
    llm: LLMClient
    browser_pool: Any
    vault: Vault
    max_tool_calls: int = 12
    deadline_seconds: float = 60.0

    async def plan(
        self,
        *,
        user_message: str,
        tenant_id: UUID,
        granted_capabilities: set[str],
        granted_capability_grants: dict[str, dict[str, Any]],
        current_dag: Dag | None = None,
        chat_history: list[LLMMessage] | None = None,
    ) -> PlannerResponse:
        """Run the agentic loop. Returns the same union as PlannerService.

        `granted_capabilities` is the set of refs (used by the prompt
        builder for visibility); `granted_capability_grants` is the full
        ref→alias→{vault_ref, …} map (used by login tool to fetch creds).
        """
        granted_aliases: dict[str, list[str]] = {
            ref: sorted(alias_map.keys())
            for ref, alias_map in (granted_capability_grants or {}).items()
        }
        grant_input_defaults: dict[str, dict[str, dict[str, Any]]] = {
            ref: {
                alias: dict(info.get("input_defaults") or {})
                for alias, info in alias_map.items()
            }
            for ref, alias_map in (granted_capability_grants or {}).items()
        }
        prompt_builder = PromptBuilder(
            registry=self.registry,
            granted_capabilities=granted_capabilities,
            granted_aliases=granted_aliases,
            grant_input_defaults=grant_input_defaults,
        )
        base_messages = prompt_builder.build_messages(
            user_message=user_message,
            current_dag=current_dag,
            chat_history=chat_history,
        )
        # Inject the agentic-mode system addendum right after the main
        # system prompt so the LLM sees the tool semantics.
        messages: list[LLMMessage] = list(base_messages)
        messages.insert(1, LLMMessage(role=Role.SYSTEM, content=_AGENTIC_SYSTEM_NOTE))

        runner = PlannerToolRunner(
            browser_pool=self.browser_pool,
            vault=self.vault,
            tenant_id=tenant_id,
            granted_capabilities=granted_capability_grants,
            registry=self.registry,
        )
        tools = all_tool_schemas()
        deadline = time.monotonic() + self.deadline_seconds
        observations: list[str] = []  # short summaries for the clarify-fallback

        logger.info(
            "agentic plan tenant_id=%s tools=%d max_calls=%d deadline=%.1fs",
            tenant_id,
            len(tools),
            self.max_tool_calls,
            self.deadline_seconds,
        )
        async with runner.session():
            for iteration in range(self.max_tool_calls):
                if time.monotonic() > deadline:
                    observations.append(f"hit time deadline after {iteration} tool calls")
                    logger.warning("agentic plan: hit deadline after %d iterations", iteration)
                    break
                try:
                    step = await asyncio.to_thread(
                        self.llm.complete_with_tools, messages, tools
                    )
                except Exception as e:  # noqa: BLE001
                    logger.exception("agentic LLM call failed")
                    return ClarifyResponse(
                        questions=[
                            f"Couldn't reach the planner LLM ({type(e).__name__}). "
                            "Try again, or give me explicit selectors."
                        ]
                    )

                if not step.tool_calls:
                    # The model gave up on tools — surface what it said as
                    # a clarify question. Better than silently looping.
                    text = step.final_content or "Planner stopped without producing a DAG."
                    logger.info("agentic: model returned final content without tools (iter=%d)", iteration)
                    return ClarifyResponse(questions=[text])
                logger.debug(
                    "agentic iter=%d tool_calls=%s",
                    iteration,
                    [c.name for c in step.tool_calls],
                )

                # Process each tool call (the LLM occasionally emits more
                # than one per turn). The `done` call short-circuits or
                # triggers an in-loop repair if the DAG fails validation.
                tool_messages: list[LLMMessage] = []
                repair_msg: str | None = None
                for call in step.tool_calls:
                    if call.name == "done":
                        try:
                            with open("/tmp/aakaar-agentic-trace.log", "a") as fh:
                                fh.write(f"---\nuser={user_message}\n")
                                for o in observations:
                                    fh.write(f"  {o}\n")
                                fh.write(f"  done.kind={call.arguments.get('kind')}\n")
                        except Exception:
                            pass
                        finalized = self._finalize(
                            call, granted_capabilities, observations
                        )
                        # If the LLM tried to emit a DAG but it didn't
                        # validate, _finalize returns a ClarifyResponse.
                        # Treat that as a repair opportunity instead of
                        # giving up: feed the error back and let the
                        # LLM fix it. Three of the most common bugs are
                        # missing edges, hallucinated selectors, and
                        # references to nodes that don't exist.
                        if (
                            call.arguments.get("kind") == "dag"
                            and isinstance(finalized, ClarifyResponse)
                            and finalized.questions
                        ):
                            repair_msg = finalized.questions[0]
                            break
                        return finalized
                    result = await dispatch(runner, call.name, call.arguments)
                    observations.append(self._summarize(call, result.payload))
                    tool_messages.append(self._format_tool_message(call, result.payload))

                # Echo the assistant's tool calls and our results back into
                # the conversation so the next call sees them in context.
                messages.append(self._format_assistant_call_record(step.tool_calls))
                if repair_msg is not None:
                    observations.append(f"repair: {repair_msg[:160]}")
                    messages.append(
                        LLMMessage(
                            role=Role.USER,
                            content=(
                                "<repair>The DAG you proposed didn't "
                                f"validate: {repair_msg}. Fix the DAG and "
                                "call done() again. Common fixes: (1) every "
                                "${node.field} reference needs an edge path "
                                "from the producing node — add missing edges; "
                                "(2) selectors must come VERBATIM from "
                                "inspect_page results — call inspect_page "
                                "again on the right page if you don't have "
                                "the selectors yet.</repair>"
                            ),
                        )
                    )
                else:
                    messages.extend(tool_messages)
            else:
                observations.append(
                    f"hit iteration cap of {self.max_tool_calls} tool calls"
                )
                logger.warning("agentic plan: hit iteration cap of %d", self.max_tool_calls)

        try:
            with open("/tmp/aakaar-agentic-trace.log", "a") as fh:
                fh.write(f"---\nuser={user_message}\n")
                for o in observations:
                    fh.write(f"  {o}\n")
        except Exception:
            pass
        return ClarifyResponse(
            questions=[
                "I explored the site but couldn't finalize the workflow within budget.",
                *observations[-4:],
                "Could you confirm the URL and the key elements (e.g. report name to download)?",
            ]
        )

    # ---------- internals ------------------------------------------------

    def _finalize(
        self,
        call: ToolCall,
        granted_capabilities: set[str],
        observations: list[str],
    ) -> PlannerResponse:
        """Convert a `done(...)` tool call into a PlannerResponse, doing
        the same DAG validation the one-shot planner does."""
        kind = call.arguments.get("kind")
        rationale = str(call.arguments.get("rationale") or "")
        if kind == "clarify":
            qs = call.arguments.get("questions") or []
            return ClarifyResponse(questions=[str(q) for q in qs] or observations[-4:])
        if kind == "missing":
            needed = call.arguments.get("needed") or []
            return MissingResponse(
                needed=[str(r) for r in needed],
                explanation=str(call.arguments.get("explanation") or ""),
            )
        if kind != "dag":
            return ClarifyResponse(questions=[f"unknown done kind: {kind!r}"])

        raw_dag = call.arguments.get("dag")
        if not isinstance(raw_dag, dict):
            return ClarifyResponse(
                questions=["done(kind='dag') was called without a dag object"]
            )
        try:
            dag = Dag.model_validate(raw_dag)
            dag = auto_complete_edges(dag)
            validate_dag(
                dag,
                registry=self.registry,
                granted_capabilities=granted_capabilities,
            )
        except ValidationError as e:
            try:
                with open("/tmp/aakaar-agentic-trace.log", "a") as fh:
                    fh.write(f"  validation_error: {e}\n")
                    import json as _j
                    fh.write(f"  dag={_j.dumps(raw_dag)[:2000]}\n")
            except Exception:
                pass
            return ClarifyResponse(
                questions=[f"the proposed DAG didn't validate: {e}"]
            )
        except Exception as e:  # noqa: BLE001
            try:
                with open("/tmp/aakaar-agentic-trace.log", "a") as fh:
                    fh.write(f"  parse_error: {e}\n")
            except Exception:
                pass
            return ClarifyResponse(questions=[f"DAG parse failed: {e}"])
        return DagResponse(dag=dag, rationale=rationale)

    def _format_tool_message(
        self, call: ToolCall, payload: dict[str, Any]
    ) -> LLMMessage:
        """Tool-result message in the conversation. We use a USER-role
        message tagged with the tool id; OpenAI's tool-calling API also
        accepts a 'tool' role, but we keep the LLMMessage protocol simple
        and let the OpenAI adapter handle role mapping."""
        body = json.dumps({"tool": call.name, "tool_call_id": call.id, "result": payload})
        return LLMMessage(role=Role.USER, content=f"<tool_result>{body}</tool_result>")

    def _format_assistant_call_record(self, calls: list[ToolCall]) -> LLMMessage:
        body = json.dumps(
            [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in calls]
        )
        return LLMMessage(
            role=Role.ASSISTANT, content=f"<tool_calls>{body}</tool_calls>"
        )

    @staticmethod
    def _summarize(call: ToolCall, payload: dict[str, Any]) -> str:
        if "error" in payload:
            return (
                f"{call.name}({json.dumps(call.arguments)[:160]}): "
                f"error — {payload['error']}"
            )
        if call.name == "navigate":
            return (
                f"navigate(url={call.arguments.get('url')!r}) → "
                f"{payload.get('url') or '?'} ({payload.get('title') or ''})"
            )
        if call.name == "login_with_grant":
            if payload.get("logged_in"):
                return (
                    f"login_with_grant({json.dumps(call.arguments)[:120]}) → "
                    f"landed on {payload.get('url') or '?'}"
                )
            return f"login_with_grant: {payload}"
        if call.name == "inspect_page":
            n = payload.get("interactive_count_total", 0)
            return f"inspected {payload.get('url') or '?'} — {n} interactive elements"
        if call.name == "click":
            clicked = payload.get("clicked") or call.arguments
            n = payload.get("interactive_count_total", 0)
            return (
                f"click({json.dumps(clicked)[:120]}) → re-inspected "
                f"{payload.get('url') or '?'} — {n} interactive elements"
            )
        return f"{call.name}: {len(json.dumps(payload))} bytes"
