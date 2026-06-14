"""Press a validated key combo on the remote desktop. — remote-only capability contract (implemented by the agent)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from aakaar.shared.registry import CapabilityDefinition


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    combo: str


class _Outputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The agent echoes the canonical combo it pressed (e.g. "ctrl+s"), not a bool.
    sent: str


definition = CapabilityDefinition(
    ref="cap.key_send",
    description="Press a validated key combo (e.g. enter, ctrl+s, alt+tab) on the remote desktop.",
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("remote", "desktop", "gui"),
)
remote_only = True
