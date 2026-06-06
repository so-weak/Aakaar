"""cap.shell_exec — run a command (argv, never a shell string) on the host."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict

from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")
    argv: list[str]
    cwd: str | None = None
    timeout_s: float = 60.0


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exit_code: int
    stdout: str
    stderr: str


SPEC = CapabilitySpec(
    ref="cap.shell_exec",
    description="Run an allow-listed command (argv) on the host and capture its output.",
    input_schema=_In,
    output_schema=_Out,
    tags=("shell", "remote"),
)


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    argv = inputs.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("shell_exec requires argv: list[str]")
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=inputs.get("cwd"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(), timeout=float(inputs.get("timeout_s", 60.0))
        )
    except TimeoutError:
        proc.kill()
        raise RuntimeError("command timed out") from None
    return {
        "exit_code": proc.returncode if proc.returncode is not None else -1,
        "stdout": out.decode("utf-8", errors="replace")[:100_000],
        "stderr": err.decode("utf-8", errors="replace")[:10_000],
    }
