"""cap.process_list — list processes on the executing host (psutil).

Enumerates running processes with psutil, optionally filtering by a substring
of the process name, and returns pid / name / cpu% / mem% / username for each
(up to ``limit``). Read-only (``side_effecting=False``). psutil is imported
lazily with a clear failure naming the pip extra.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.process_list"


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name_contains: str | None = Field(
        default=None,
        description="Case-insensitive substring to filter process names. Omit to list all.",
    )
    limit: int = Field(default=100, ge=1, le=10000, description="Maximum number of processes to return.")


class _Process(BaseModel):
    pid: int
    name: str
    cpu_percent: float | None = None
    mem_percent: float | None = None
    username: str | None = None


class _Outputs(BaseModel):
    processes: list[_Process] = Field(description="Matching processes (pid, name, cpu%, mem%, username).")
    count: int = Field(description="Number of processes returned.")


SPEC = CapabilitySpec(
    ref=CAP_REF,
    description=(
        "List processes on the executing host with psutil, optionally filtered by a "
        "case-insensitive substring of the process name, returning pid / name / cpu% / "
        "mem% / username for each (up to a limit). Read-only."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("host", "process"),
    side_effecting=False,
)


def _require_psutil() -> Any:
    try:
        import psutil  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - exercised only when dep absent
        raise RuntimeError(
            "cap.process_list needs psutil — install aakaar-capabilities[automation]"
        ) from exc
    return psutil


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    psutil = _require_psutil()
    needle = (inputs.get("name_contains") or "").lower()
    limit = int(inputs.get("limit", 100))

    procs: list[dict[str, Any]] = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "username"]):
        try:
            info = p.info
            name = info.get("name") or ""
            if needle and needle not in name.lower():
                continue
            procs.append({
                "pid": int(info["pid"]),
                "name": name,
                "cpu_percent": info.get("cpu_percent"),
                "mem_percent": (round(info["memory_percent"], 2) if info.get("memory_percent") is not None else None),
                "username": info.get("username"),
            })
            if len(procs) >= limit:
                break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    logger.info("cap.process_list ok run_id=%s filter=%r count=%d", ctx.run_id, needle, len(procs))
    return {"processes": procs, "count": len(procs)}
