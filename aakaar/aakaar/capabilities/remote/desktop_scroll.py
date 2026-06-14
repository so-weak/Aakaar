"""Scroll the focused window on the remote desktop. — remote-only capability contract (implemented by the agent)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from aakaar.shared.registry import CapabilityDefinition


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dx: int = 0
    dy: int = 0


class _Outputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scrolled: bool
    dx: int
    dy: int


definition = CapabilityDefinition(
    ref="cap.desktop_scroll",
    description="Scroll the focused window on the remote desktop by wheel notches.",
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("remote", "desktop", "gui"),
)
remote_only = True
