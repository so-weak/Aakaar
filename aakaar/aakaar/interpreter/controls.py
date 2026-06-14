"""Operator run controls — pause / resume / cancel coordination.

`ControlHub` is the in-process coordinator for operator-initiated lifecycle
actions, mirroring `SignalHub`: the orchestrator registers a
`RunControlHandle` when it schedules a run, the API endpoints flip the
handle, and the executor consults it at every DAG layer boundary.

Two independent mechanisms can hold a run still:

  1. Operator pause (this module) — clears the handle's layer gate. Nodes
     already in flight finish; no new layer starts until the gate reopens.
  2. human.prompt wait (`signals.SignalHub`) — a control node *inside* a
     layer awaits a user-response future.

Precedence rule: the two causes never release each other. Resuming an
operator pause reopens only the layer gate — a pending human prompt keeps
its node (and therefore the run) waiting until POST /runs/{id}/respond
resolves it. Conversely, answering a prompt never reopens an operator-
paused gate; the run still stops before its next layer. Cancellation
overrides both: it opens the gate and the orchestrator cancels pending
prompt futures so the run can unwind.

In-process only — works for single-node deployments, same caveat as
SignalHub.

Provenance: the operator pause/resume/cancel concept and its layer-boundary
gate were studied from the diverged Aakaar-Ravi fork, which holds the gate as
a raw `asyncio.Event` map on the orchestrator and cancels via `task.cancel()`.
This `ControlHub`/`RunControlHandle` model is an independent redesign:
pause/cancel coexist on one handle with explicit conflict detection
(`RunControlConflict`), cancel is cooperative (`RunCancelled` raised and
unwound by the run, not a hard task cancel), and the prompt-vs-pause
precedence is made an invariant rather than incidental behavior.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field


class RunCancelled(Exception):
    """Raised inside the executor when an operator cancel is observed."""


class RunNotActive(KeyError):
    """No live control handle — the run is not executing in this process."""


class RunControlConflict(Exception):
    """The requested transition is invalid for the run's current control
    state (already paused, not paused, waiting on a prompt, ...). The
    message is safe to surface to the API caller."""


@dataclass(slots=True)
class RunControlHandle:
    """Mutable control state for one scheduled run."""

    run_id: uuid.UUID
    tenant_id: uuid.UUID
    gate: asyncio.Event
    """Set = the run may advance to its next layer. Cleared by pause()."""
    cancel_event: asyncio.Event
    """Set once an operator cancel has been requested. Never cleared."""

    @property
    def paused(self) -> bool:
        return not self.gate.is_set()

    @property
    def cancel_requested(self) -> bool:
        return self.cancel_event.is_set()

    def pause(self) -> None:
        self.gate.clear()

    def resume(self) -> None:
        self.gate.set()

    def request_cancel(self) -> None:
        self.cancel_event.set()
        # Open the gate too, so a paused run wakes up and reaches the
        # cancellation check instead of sleeping at the gate forever.
        self.gate.set()

    async def checkpoint(self) -> None:
        """Layer-boundary control point: block while paused, raise once
        cancelled (checked on both sides of the gate wait)."""
        if self.cancel_event.is_set():
            raise RunCancelled(f"run {self.run_id} cancelled")
        await self.gate.wait()
        if self.cancel_event.is_set():
            raise RunCancelled(f"run {self.run_id} cancelled")


@dataclass
class ControlHub:
    """In-memory map of run_id -> RunControlHandle.

    No lock: every mutation is a plain dict/Event operation issued from the
    single event loop with no awaits in between, so calls can't interleave.
    """

    _handles: dict[uuid.UUID, RunControlHandle] = field(default_factory=dict)

    def register(self, run_id: uuid.UUID, tenant_id: uuid.UUID) -> RunControlHandle:
        gate = asyncio.Event()
        gate.set()
        handle = RunControlHandle(
            run_id=run_id, tenant_id=tenant_id, gate=gate, cancel_event=asyncio.Event()
        )
        self._handles[run_id] = handle
        return handle

    def get(self, run_id: uuid.UUID) -> RunControlHandle:
        handle = self._handles.get(run_id)
        if handle is None:
            raise RunNotActive(str(run_id))
        return handle

    def peek(self, run_id: uuid.UUID) -> RunControlHandle | None:
        return self._handles.get(run_id)

    def discard(self, run_id: uuid.UUID) -> None:
        self._handles.pop(run_id, None)
