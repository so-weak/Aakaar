"""Three-way response shape for the NL→DAG planner.

The planner is bound to one of three honest answers:

  - dag:      "I have a workflow that fulfills your request."
  - clarify:  "I need a few specifics before I can build a workflow."
  - missing:  "No combination of available capabilities can do this."

`missing` is what the planner returns when the user asks for something that
no granted capability + primitive composition can deliver. This is how the
system stays honest: rather than improvising a plausible-looking DAG, it
surfaces the gap to the user with the specific capability name(s) that would
unblock them.

These shapes are also the OpenAI Structured Outputs JSON Schema served to the
model. Pydantic models double as validators and schema sources.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from aakaar.shared.dag.types import Dag


class PlannerResponseKind(StrEnum):
    DAG = "dag"
    CLARIFY = "clarify"
    MISSING = "missing"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DagResponse(_Strict):
    kind: Literal[PlannerResponseKind.DAG] = PlannerResponseKind.DAG
    dag: Dag
    rationale: str = Field(
        default="",
        description="Short plain-English summary of what the workflow will do, for the chat UI.",
    )


class ClarifyResponse(_Strict):
    kind: Literal[PlannerResponseKind.CLARIFY] = PlannerResponseKind.CLARIFY
    questions: list[str] = Field(
        min_length=1,
        description="Specific questions to ask the user. NEVER ask for credentials.",
    )


class MissingResponse(_Strict):
    kind: Literal[PlannerResponseKind.MISSING] = PlannerResponseKind.MISSING
    needed: list[str] = Field(
        min_length=1,
        description=(
            "Capability refs that, if granted, would let the planner fulfill the request. "
            "May include refs that don't exist yet — that's how Aakaar staff prioritize new "
            "capability authoring."
        ),
    )
    explanation: str = Field(
        description="Plain-English explanation of why the request can't be fulfilled today."
    )


PlannerResponse = Annotated[
    DagResponse | ClarifyResponse | MissingResponse,
    Field(discriminator="kind"),
]
