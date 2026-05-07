"""Human-prompt signal coordination.

When a `human.prompt` node fires, the executor needs to:
  1. Mark the run paused.
  2. Surface the prompt via a run event.
  3. Wait for a response from the user (delivered by an HTTP endpoint).
  4. Resume with the user's text as the node's `response` output.

The `SignalHub` is the in-process coordinator. It maps (run_id, node_id)
to a future the executor awaits; the API endpoint resolves the future.

This is in-process only — works for single-node deployments. The Temporal
swap-in replaces SignalHub with `workflow.wait_condition` + signal handlers.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Literal


SignalExpects = Literal["text", "otp", "confirm"]


@dataclass(slots=True)
class PendingPrompt:
    run_id: uuid.UUID
    node_id: str
    message: str
    expects: SignalExpects
    future: asyncio.Future[str]


class SignalNotPending(KeyError):
    """No prompt is waiting for this (run_id, node_id)."""


@dataclass
class SignalHub:
    """In-memory map of run_id -> { node_id -> PendingPrompt }."""

    _pending: dict[uuid.UUID, dict[str, PendingPrompt]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def open(
        self,
        *,
        run_id: uuid.UUID,
        node_id: str,
        message: str,
        expects: SignalExpects,
    ) -> PendingPrompt:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        async with self._lock:
            bucket = self._pending.setdefault(run_id, {})
            if node_id in bucket:
                raise ValueError(f"prompt already pending for {run_id}/{node_id}")
            prompt = PendingPrompt(
                run_id=run_id, node_id=node_id, message=message,
                expects=expects, future=future,
            )
            bucket[node_id] = prompt
        return prompt

    async def resolve(self, run_id: uuid.UUID, node_id: str, response: str) -> None:
        async with self._lock:
            bucket = self._pending.get(run_id) or {}
            prompt = bucket.pop(node_id, None)
            if prompt is None:
                raise SignalNotPending(f"{run_id}/{node_id}")
            if not bucket:
                self._pending.pop(run_id, None)
        if not prompt.future.done():
            prompt.future.set_result(response)

    async def cancel_all_for(self, run_id: uuid.UUID) -> None:
        async with self._lock:
            bucket = self._pending.pop(run_id, None) or {}
        for prompt in bucket.values():
            if not prompt.future.done():
                prompt.future.cancel()

    def list_pending(self, run_id: uuid.UUID) -> list[PendingPrompt]:
        return list((self._pending.get(run_id) or {}).values())
