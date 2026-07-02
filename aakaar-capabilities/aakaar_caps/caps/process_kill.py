"""cap.process_kill — terminate a process by pid or name on the host (psutil).

Kills the process with the given ``pid``, or every process whose name matches
``name`` (case-insensitive substring), gracefully (SIGTERM/terminate) by default
or forcibly (SIGKILL/kill) when ``graceful=False``. Mutates the host, so
``side_effecting=True``. Guard: refuses pid <= 1 (init/kernel). psutil lazy.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.process_kill"


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pid: int | None = Field(default=None, description="Exact pid to kill. Must be > 1. Use this OR name.")
    name: str | None = Field(
        default=None,
        description="Case-insensitive substring of process names to kill. Use this OR pid.",
    )
    graceful: bool = Field(
        default=True,
        description="True -> terminate (SIGTERM); False -> kill (SIGKILL, forced).",
    )

    @model_validator(mode="after")
    def _one_target(self) -> "_Inputs":
        if (self.pid is None) == (self.name is None):
            raise ValueError("provide exactly one of pid or name")
        return self


class _Outputs(BaseModel):
    killed: list[int] = Field(description="Pids that were signalled to terminate.")
    not_found: bool = Field(description="True if no matching process was found.")


SPEC = CapabilitySpec(
    ref=CAP_REF,
    description=(
        "Terminate a process on the executing host with psutil — by exact pid, or by a "
        "case-insensitive substring of the process name (kills all matches). Graceful "
        "(SIGTERM) by default, forced (SIGKILL) when graceful=False. Refuses pid<=1. "
        "Mutates the host."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("host", "process"),
    side_effecting=True,
)


def _require_psutil() -> Any:
    try:
        import psutil  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - exercised only when dep absent
        raise RuntimeError(
            "cap.process_kill needs psutil — install aakaar-capabilities[automation]"
        ) from exc
    return psutil


def _terminate(proc: Any, graceful: bool) -> None:
    if graceful:
        proc.terminate()
    else:
        proc.kill()


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    psutil = _require_psutil()
    pid = inputs.get("pid")
    name = inputs.get("name")
    graceful = bool(inputs.get("graceful", True))

    if (pid is None) == (name is None):
        raise ValueError("provide exactly one of pid or name")

    killed: list[int] = []

    if pid is not None:
        pid = int(pid)
        if pid <= 1:
            raise ValueError(f"refusing to kill pid {pid} (<= 1 is init/kernel)")
        try:
            proc = psutil.Process(pid)
            _terminate(proc, graceful)
            killed.append(pid)
        except psutil.NoSuchProcess:
            pass
    else:
        needle = str(name).lower()
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if p.info["pid"] <= 1:
                    continue
                if needle in (p.info.get("name") or "").lower():
                    _terminate(p, graceful)
                    killed.append(int(p.info["pid"]))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    not_found = not killed
    logger.info("cap.process_kill run_id=%s graceful=%s killed=%s not_found=%s",
                ctx.run_id, graceful, killed, not_found)
    return {"killed": killed, "not_found": not_found}
