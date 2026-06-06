"""Type text into the focused window on the remote desktop. — remote-only capability contract (implemented by the agent)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from aakaar.shared.registry import CapabilityDefinition


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    interval_ms: int = 0


class _Outputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    typed: int


definition = CapabilityDefinition(
    ref="cap.desktop_type",
    description="Type text into the focused window on the remote desktop.",
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("remote", "desktop", "gui"),
)
remote_only = True
