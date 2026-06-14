"""List or manipulate windows on the remote desktop. — remote-only capability contract (implemented by the agent)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from aakaar.shared.registry import CapabilityDefinition


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    title: str | None = None


class _Outputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    windows: list[Any] | None = None


definition = CapabilityDefinition(
    ref="cap.window_manage",
    description="List or manipulate windows on the remote desktop.",
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("remote", "window", "gui"),
)
remote_only = True
