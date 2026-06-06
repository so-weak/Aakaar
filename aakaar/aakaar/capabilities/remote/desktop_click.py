"""Click on the remote desktop by coordinates or matched image. — remote-only capability contract (implemented by the agent)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from aakaar.shared.registry import CapabilityDefinition


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: int | None = None
    y: int | None = None
    image: str | None = None
    button: str = "left"


class _Outputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clicked: bool


definition = CapabilityDefinition(
    ref="cap.desktop_click",
    description="Click on the remote desktop by coordinates or matched image.",
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("remote", "desktop", "gui"),
)
remote_only = True
