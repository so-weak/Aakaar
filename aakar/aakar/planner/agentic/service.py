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

from aakar.planner.agentic.runner import PlannerToolRunner
from aakar.planner.agentic.tools import (
    all_tool_schemas,
    dispatch,
)
from aakar.planner.llm import LLMClient, LLMMessage, PlannerCompletion, Role, ToolCall
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
from aakar.vault import Vault


logger = logging.getLogger(__name__)


_AGENTIC_SYSTEM_NOTE = """\
# Agentic mode

You have browser tools (navigate, inspect_page, login_with_grant) plus
`done(...)` to finalize. Use them to figure out what the workflow should
do, then call `done` ONCE with a complete plan.

Rules:
- Inspect a page BEFORE picking selectors for it. The selectors you
  emit in the final DAG must match what the page actually has.
- If the user mentioned a specific element ("first report", "Biller
  Transactions"), find it by inspecting the relevant page; don't guess.
- Login via `login_with_grant`: credentials come from the vault, never
  from chat. Default `account_alias` to "primary" if the user didn't
  say. If a captcha is detected, do NOT try to log in at plan time —
  emit a DAG that uses `cap.web_login` (which handles captchas at run
  time via HITL).
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
        prompt_builder = PromptBuilder(
            registry=self.registry, granted_capabilities=granted_capabilities
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

        async with runner.session():
            for iteration in range(self.max_tool_calls):
                if time.monotonic() > deadline:
                    observations.append(f"hit time deadline after {iteration} tool calls")
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
                    return ClarifyResponse(questions=[text])

                # Process each tool call (the LLM occasionally emits more
                # than one per turn). The `done` call short-circuits.
                tool_messages: list[LLMMessage] = []
                for call in step.tool_calls:
                    if call.name == "done":
                        return self._finalize(call, granted_capabilities, observations)
                    result = await dispatch(runner, call.name, call.arguments)
                    observations.append(self._summarize(call, result.payload))
                    tool_messages.append(self._format_tool_message(call, result.payload))

                # Echo the assistant's tool calls and our results back into
                # the conversation so the next call sees them in context.
                messages.append(self._format_assistant_call_record(step.tool_calls))
                messages.extend(tool_messages)
            else:
                observations.append(
                    f"hit iteration cap of {self.max_tool_calls} tool calls"
                )

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
            validate_dag(
                dag,
                registry=self.registry,
                granted_capabilities=granted_capabilities,
            )
        except ValidationError as e:
            return ClarifyResponse(
                questions=[f"the proposed DAG didn't validate: {e}"]
            )
        except Exception as e:  # noqa: BLE001
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
            return f"{call.name}: error — {payload['error']}"
        if call.name == "navigate":
            return f"navigated to {payload.get('url') or '?'} ({payload.get('title') or ''})"
        if call.name == "login_with_grant":
            if payload.get("logged_in"):
                return f"logged in; landed on {payload.get('url') or '?'}"
            return f"login_with_grant: {payload}"
        if call.name == "inspect_page":
            n = payload.get("interactive_count_total", 0)
            return f"inspected {payload.get('url') or '?'} — {n} interactive elements"
        return f"{call.name}: {len(json.dumps(payload))} bytes"
