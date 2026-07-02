"""cap.host_info — report the executing host's OS / CPU / memory / disk / uptime.

Prefers psutil for CPU%, memory and boot time; degrades gracefully to the
platform/os stdlib when psutil is absent (never hard-fails just for missing
psutil — the memory/cpu_percent/boot_time fields simply come back None).
Read-only (``side_effecting=False``). Distinct from cap.system_info: this
returns a richer, structured host report (hostname, cpu_count, disk_usage,
boot_time).
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import socket
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.host_info"


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    disk_path: str = Field(default="/", description="Filesystem path to report disk usage for.")


class _Outputs(BaseModel):
    os: str = Field(description="Operating system name (platform.system()).")
    hostname: str = Field(description="Host name.")
    cpu_percent: float | None = Field(default=None, description="System-wide CPU utilisation % (psutil; None if absent).")
    cpu_count: int | None = Field(default=None, description="Logical CPU count.")
    mem_total: int | None = Field(default=None, description="Total physical memory in bytes (psutil).")
    mem_available: int | None = Field(default=None, description="Available memory in bytes (psutil).")
    disk_usage: dict[str, Any] = Field(description="{path,total,used,free,percent} for disk_path.")
    boot_time: float | None = Field(default=None, description="System boot time as a POSIX timestamp (psutil).")


SPEC = CapabilitySpec(
    ref=CAP_REF,
    description=(
        "Report the executing host's structured system info: OS, hostname, CPU count and "
        "utilisation %, total/available memory, disk usage for a given path, and boot time. "
        "Uses psutil when present and degrades to stdlib otherwise. Read-only."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("host", "system"),
    side_effecting=False,
)


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    disk_path = str(inputs.get("disk_path", "/"))
    out: dict[str, Any] = {
        "os": platform.system(),
        "hostname": socket.gethostname(),
        "cpu_percent": None,
        "cpu_count": os.cpu_count(),
        "mem_total": None,
        "mem_available": None,
        "disk_usage": {},
        "boot_time": None,
    }

    try:
        import psutil  # type: ignore[import-untyped]

        out["cpu_percent"] = psutil.cpu_percent(interval=0.1)
        out["cpu_count"] = psutil.cpu_count(logical=True) or out["cpu_count"]
        vm = psutil.virtual_memory()
        out["mem_total"] = int(vm.total)
        out["mem_available"] = int(vm.available)
        out["boot_time"] = float(psutil.boot_time())
    except Exception:  # noqa: BLE001 — psutil optional / may raise on odd hosts
        logger.debug("cap.host_info: psutil unavailable, degrading to stdlib", exc_info=True)

    try:
        du = shutil.disk_usage(disk_path)
        pct = round(du.used / du.total * 100, 1) if du.total else 0.0
        out["disk_usage"] = {"path": disk_path, "total": du.total, "used": du.used, "free": du.free, "percent": pct}
    except Exception:  # noqa: BLE001
        out["disk_usage"] = {"path": disk_path, "total": 0, "used": 0, "free": 0, "percent": 0.0}

    logger.info("cap.host_info ok run_id=%s os=%s host=%s", ctx.run_id, out["os"], out["hostname"])
    return out
