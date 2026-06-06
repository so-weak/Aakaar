"""cap.json_extract — pull a value out of JSON by a dotted path. Pure compute,
runs anywhere (good for transforming local data on a remote agent)."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict

from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data: str
    path: str = ""


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Any


SPEC = CapabilitySpec(
    ref="cap.json_extract",
    description="Parse a JSON string and return the value at a dotted path (e.g. 'a.b.0.c').",
    input_schema=_In,
    output_schema=_Out,
    tags=("data", "json"),
)


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    parsed = json.loads(inputs.get("data", "null"))
    path = inputs.get("path", "") or ""
    cur: Any = parsed
    for seg in [p for p in path.split(".") if p != ""]:
        if isinstance(cur, list):
            cur = cur[int(seg)]
        elif isinstance(cur, dict):
            cur = cur[seg]
        else:
            raise KeyError(f"cannot descend into {type(cur).__name__} at {seg!r}")
    return {"value": cur}
