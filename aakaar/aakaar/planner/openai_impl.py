"""OpenAI implementation of the LLM protocol.

Thin adapter — the planner doesn't depend on it directly, only on the
Protocol in `llm.py`. Wiring code chooses the concrete impl at startup.
Embeddings are served by `BGEEmbeddingsClient` (see `hf_impl.py`), not
the OpenAI embeddings API.

We use JSON mode (`response_format={"type": "json_object"}`) rather than
strict structured outputs because `Node.inputs: dict[str, Any]` is open-
ended, which strict mode cannot express. The envelope shape is described
to the model in the system prompt; we validate the returned JSON against
`PlannerCompletion` here so a malformed reply fails fast and feeds back
into the planner's repair loop.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, cast

from openai import OpenAI
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionFunctionToolParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionUserMessageParam,
)
from openai.types.shared_params import FunctionDefinition
from pydantic import ValidationError as PydanticValidationError

from aakaar.planner.llm import (
    LLMClient,
    LLMMessage,
    PlannerCompletion,
    Role,
    ToolCall,
    ToolStep,
)
from aakaar.shared.dag import ValidationError

logger = logging.getLogger(__name__)
_DEFAULT_LLM_MODEL = "gpt-5.4-mini"

# Compact reminder of the envelope shape, fed back on a malformed completion so
# the model knows EXACTLY what to produce instead of a generic "invalid" nudge.
_ENVELOPE_HINT = (
    '{"kind": "dag"|"clarify"|"missing", "rationale": "...", '
    '"dag": {...}|null, "questions": [...], "needed": [...], "explanation": "..."} '
    "(exactly one branch populated per `kind`; no markdown, no code fences)."
)


def _summarize_pydantic_errors(exc: PydanticValidationError) -> str:
    """Turn pydantic's error list into a short, model-actionable sentence
    naming the offending fields and what was wrong — a much stronger repair
    signal than the raw multi-line dump."""
    try:
        parts: list[str] = []
        for err in exc.errors()[:5]:
            loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
            msg = err.get("msg", "invalid")
            parts.append(f"{loc}: {msg}")
        if not parts:
            return "The JSON did not match the expected schema."
        return "Problems: " + "; ".join(parts) + "."
    except Exception:  # pragma: no cover - defensive; never let repair hint crash
        return "The JSON did not match the expected schema."


def _to_message_params(
    messages: list[LLMMessage],
) -> list[ChatCompletionMessageParam]:
    """Map our role-tagged messages onto OpenAI's per-role TypedDict params
    so the typed `create()` overloads accept them. `Role` only ever carries
    system/user/assistant, matching the three concrete param types."""
    out: list[ChatCompletionMessageParam] = []
    for m in messages:
        if m.role is Role.SYSTEM:
            out.append(
                ChatCompletionSystemMessageParam(role="system", content=m.content)
            )
        elif m.role is Role.ASSISTANT:
            out.append(
                ChatCompletionAssistantMessageParam(
                    role="assistant", content=m.content
                )
            )
        else:
            out.append(
                ChatCompletionUserMessageParam(role="user", content=m.content)
            )
    return out


def _to_tool_params(
    tools: list[dict[str, Any]],
) -> list[ChatCompletionFunctionToolParam]:
    return [
        ChatCompletionFunctionToolParam(
            type="function", function=cast(FunctionDefinition, t)
        )
        for t in tools
    ]


@dataclass
class OpenAILLMClient(LLMClient):
    client: OpenAI
    model: str = _DEFAULT_LLM_MODEL
    temperature: float = 0.0

    def complete_planner(self, messages: list[LLMMessage]) -> PlannerCompletion:
        logger.debug("OpenAI complete_planner model=%s messages=%d", self.model, len(messages))
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            messages=_to_message_params(messages),
            response_format={"type": "json_object"},
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            logger.debug(
                "OpenAI tokens prompt=%s completion=%s total=%s",
                getattr(usage, "prompt_tokens", "?"),
                getattr(usage, "completion_tokens", "?"),
                getattr(usage, "total_tokens", "?"),
            )
        content = response.choices[0].message.content
        if not content:
            # Empty content is repairable too — surface as a ValidationError so
            # the planner's repair loop feeds a concrete instruction back rather
            # than 502-ing on the first empty reply.
            logger.warning("OpenAI returned an empty completion")
            raise ValidationError(
                "Your previous reply was empty. Respond with a single JSON object "
                f"conforming to the response schema: {_ENVELOPE_HINT}"
            )
        try:
            return PlannerCompletion.model_validate_json(content)
        except PydanticValidationError as e:
            # Surface as a DAG-layer ValidationError so the planner's repair
            # loop (`PlannerService.plan`) sees it, feeds a SPECIFIC error back
            # to the model, and gets another chance to produce well-formed JSON.
            # The concrete pydantic errors + a reminder of the exact envelope
            # shape are a far stronger repair signal than a generic "invalid".
            logger.warning("PlannerCompletion JSON validation failed: %s", e)
            raise ValidationError(
                "Your previous reply was not a valid PlannerCompletion. "
                f"{_summarize_pydantic_errors(e)} "
                f"Return ONLY a single JSON object matching: {_ENVELOPE_HINT}"
            ) from e

    def complete_with_tools(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]],
    ) -> ToolStep:
        """One round of tool-calling for agentic planning. Tools are passed
        in OpenAI's function-call schema; the response is either zero or
        more tool calls (continue the loop) or a final text content (the
        model is trying to talk to the user — agentic service treats this
        as a hint to give up and produce a clarify response)."""
        logger.debug(
            "OpenAI complete_with_tools model=%s messages=%d tools=%d",
            self.model,
            len(messages),
            len(tools),
        )
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            messages=_to_message_params(messages),
            tools=_to_tool_params(tools),
            # Letting the model choose: if it has nothing to do, we want a
            # final-content reply so we can break the loop cleanly.
            tool_choice="auto",
        )
        msg = response.choices[0].message
        raw_calls = getattr(msg, "tool_calls", None) or []
        logger.debug(
            "OpenAI tool step tool_calls=%d has_final_content=%s",
            len(raw_calls),
            bool(msg.content),
        )
        calls: list[ToolCall] = []
        for tc in raw_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tc.function.arguments}
            if not isinstance(args, dict):
                args = {"_value": args}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
        return ToolStep(tool_calls=calls, final_content=msg.content)

    def complete_text(self, system: str, user: str) -> str:
        """Single-shot free-text completion for capability-time extraction."""
        logger.debug("OpenAI complete_text model=%s", self.model)
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            messages=[
                ChatCompletionSystemMessageParam(role="system", content=system),
                ChatCompletionUserMessageParam(role="user", content=user),
            ],
        )
        return response.choices[0].message.content or ""
