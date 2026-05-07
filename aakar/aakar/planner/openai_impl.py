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

from dataclasses import dataclass

from openai import OpenAI
from pydantic import ValidationError

from aakar.planner.llm import LLMClient, LLMMessage, PlannerCompletion


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
        except ValidationError as e:
            raise RuntimeError(f"OpenAI returned invalid PlannerCompletion JSON: {e}") from e
