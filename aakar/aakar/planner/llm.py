"""LLM client abstraction for the planner.

The planner depends on a `LLMClient` Protocol so it can be tested without
hitting OpenAI. The OpenAI implementation lives in `openai_impl.py` and
implements this Protocol; tests use `FakeLLMClient` with scripted replies.

The Planner asks the model for a single object — `PlannerCompletion` —
which carries one of three discriminated payloads (dag/clarify/missing).
This is a flat envelope rather than a discriminated-union top-level because
OpenAI's strict structured-outputs mode handles single concrete classes
with optional fields more reliably than top-level oneOf schemas.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aakar.shared.dag.types import Dag


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class LLMMessage:
    role: Role
    content: str


# ---------- planner completion envelope ------------------------------------


class PlannerCompletion(BaseModel):
    """Flat envelope returned by the LLM.

    Exactly one of (dag, clarify-questions, missing-needed/explanation) must
    be populated, governed by `kind`. The model_validator below enforces
    that constraint so a malformed completion fails fast and feeds back into
    the repair loop.

    `rationale` is always allowed (and encouraged) — a one- or two-line
    English summary the chat UI shows alongside any of the three outcomes.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["dag", "clarify", "missing"]
    rationale: str = Field(
        default="",
        description="Plain-English summary of the planner's response. Always allowed.",
    )

    # dag branch
    dag: Dag | None = Field(
        default=None,
        description="The workflow DAG. Required iff kind == 'dag'.",
    )

    # clarify branch
    questions: list[str] = Field(
        default_factory=list,
        description=(
            "Questions to ask the user. Required iff kind == 'clarify'. "
            "NEVER ask for credentials, OTPs, or any secrets."
        ),
    )

    # missing branch
    needed: list[str] = Field(
        default_factory=list,
        description=(
            "Capability refs that, if granted, would unblock the request. "
            "Required iff kind == 'missing'. May include refs that don't exist yet — "
            "those are signals for staff to author new capabilities."
        ),
    )
    explanation: str = Field(
        default="",
        description="Plain-English explanation. Required iff kind == 'missing'.",
    )

    @model_validator(mode="after")
    def _check_branch_invariants(self) -> "PlannerCompletion":
        if self.kind == "dag":
            if self.dag is None:
                raise ValueError("kind=='dag' requires `dag` to be set")
            if self.questions or self.needed or self.explanation:
                raise ValueError("kind=='dag' must not set clarify/missing fields")
        elif self.kind == "clarify":
            if not self.questions:
                raise ValueError("kind=='clarify' requires at least one question")
            if self.dag is not None or self.needed or self.explanation:
                raise ValueError("kind=='clarify' must not set dag/missing fields")
        elif self.kind == "missing":
            if not self.needed:
                raise ValueError("kind=='missing' requires at least one needed ref")
            if not self.explanation:
                raise ValueError("kind=='missing' requires an explanation")
            if self.dag is not None or self.questions:
                raise ValueError("kind=='missing' must not set dag/clarify fields")
        return self


# ---------- protocol -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool call requested by the LLM in agentic planning mode."""

    id: str
    """Provider-specific id; we echo it back when returning the result."""
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolStep:
    """One step the agentic planner takes. Either the LLM asked for a tool
    (continue the loop), or it produced final content (done)."""

    tool_calls: list[ToolCall]
    final_content: str | None
    """Set when the LLM didn't call a tool — i.e., it's trying to talk to
    the user instead of acting. The agentic service treats this as a hint
    that the model wants to give up on tools and produce a clarify reply."""


class LLMClient(Protocol):
    """Minimal LLM client protocol the planner depends on.

    `complete_planner` — single-shot JSON-mode plan: send the conversation,
    return a parsed `PlannerCompletion`. The default planner uses this.

    `complete_with_tools` — one round of tool-calling (agentic mode).
    Send the conversation + tool schemas, receive a tool call (or final
    content). The agentic service drives the loop.

    Any provider-specific knobs (model, temperature) are bound at
    construction time, not per-call — the planner has one job and shouldn't
    be tuning the model on the fly.
    """

    def complete_planner(self, messages: list[LLMMessage]) -> PlannerCompletion: ...

    def complete_with_tools(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]],
    ) -> ToolStep: ...


# ---------- fake -----------------------------------------------------------


@dataclass
class FakeLLMClient:
    """Test double. `replies` is a queue of pre-baked completions for the
    one-shot planner; `tool_steps` is a parallel queue for the agentic
    planner's tool-calling loop. Each call pops the next entry; an
    exhausted queue raises.

    Records call history in `calls` / `tool_calls_history` so tests can
    assert on what was sent.
    """

    replies: list[PlannerCompletion] = field(default_factory=list)
    tool_steps: list["ToolStep"] = field(default_factory=list)
    calls: list[list[LLMMessage]] = field(default_factory=list)
    tool_call_history: list[tuple[list[LLMMessage], list[dict[str, Any]]]] = field(
        default_factory=list
    )

    def complete_planner(self, messages: list[LLMMessage]) -> PlannerCompletion:
        self.calls.append(list(messages))
        if not self.replies:
            raise RuntimeError("FakeLLMClient: reply queue exhausted")
        return self.replies.pop(0)

    def complete_with_tools(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]],
    ) -> "ToolStep":
        self.tool_call_history.append((list(messages), list(tools)))
        if not self.tool_steps:
            raise RuntimeError("FakeLLMClient: tool_steps queue exhausted")
        return self.tool_steps.pop(0)

    def __iter__(self) -> Iterator[PlannerCompletion]:
        # Convenience for tests that want to inspect the queue without popping.
        return iter(self.replies)
