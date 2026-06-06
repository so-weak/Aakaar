"""Write text to the remote machine clipboard. — remote-only capability contract (implemented by the agent)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from aakaar.shared.registry import CapabilityDefinition


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


class _Outputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool


definition = CapabilityDefinition(
    ref="cap.clipboard_write",
    description="Write text to the remote machine clipboard.",
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("remote", "clipboard", "gui"),
)
remote_only = True
