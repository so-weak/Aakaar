"""cap.system_info — report the host's OS / CPU / memory / disk."""

from __future__ import annotations

import platform
from typing import Any

from pydantic import BaseModel, ConfigDict

from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")
    os: str
    cpu_percent: float | None = None
    memory: dict | None = None
    disk: dict | None = None


SPEC = CapabilitySpec(
    ref="cap.system_info",
    description="Report the host's operating system and CPU/memory/disk usage.",
    input_schema=_In,
    output_schema=_Out,
    tags=("system", "remote"),
)


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"os": platform.system(), "cpu_percent": None, "memory": None, "disk": None}
    try:
        import psutil

        out["cpu_percent"] = psutil.cpu_percent(interval=0.1)
        vm = psutil.virtual_memory()
        out["memory"] = {"total": vm.total, "available": vm.available, "percent": vm.percent}
        du = psutil.disk_usage("/")
        out["disk"] = {"total": du.total, "free": du.free, "percent": du.percent}
    except Exception:
        pass
    return out
