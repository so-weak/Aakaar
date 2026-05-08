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
from dataclasses import dataclass
from typing import Any

from openai import OpenAI
from pydantic import ValidationError as PydanticValidationError

from aakar.planner.llm import (
    LLMClient,
    LLMMessage,
    PlannerCompletion,
    ToolCall,
    ToolStep,
)
from aakar.shared.dag import ValidationError


_DEFAULT_LLM_MODEL = "gpt-4.1-mini"


@dataclass
class OpenAILLMClient(LLMClient):
    client: OpenAI
    model: str = _DEFAULT_LLM_MODEL
    temperature: float = 0.0

    def complete_planner(self, messages: list[LLMMessage]) -> PlannerCompletion:
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            messages=[{"role": m.role.value, "content": m.content} for m in messages],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("OpenAI returned an empty completion")
        try:
            return PlannerCompletion.model_validate_json(content)
        except PydanticValidationError as e:
            # Surface as a DAG-layer ValidationError so the planner's repair
            # loop (`PlannerService.plan`) sees it, feeds the error back to
            # the model, and gets another chance to produce well-formed JSON.
            raise ValidationError(
                f"PlannerCompletion JSON did not match the expected envelope: {e}"
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
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            messages=[
                {"role": m.role.value, "content": m.content} for m in messages
            ],
            tools=[{"type": "function", "function": t} for t in tools],
            # Letting the model choose: if it has nothing to do, we want a
            # final-content reply so we can break the loop cleanly.
            tool_choice="auto",
        )
        msg = response.choices[0].message
        raw_calls = getattr(msg, "tool_calls", None) or []
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
