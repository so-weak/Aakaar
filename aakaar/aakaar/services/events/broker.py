"""In-process run-event broker (no Redis).

A single-process pub/sub: WebSocket handlers subscribe per run_id and receive
events as they are recorded. ``BroadcastingEventRecorder`` wraps the canonical
recorder (which persists to the DB) and also publishes each event to the
broker, so live sockets get a push while the DB stays the source of truth.

Single event loop: ``publish`` is sync (called from the recorder inside the
executor's tasks) and ``subscribe``/consumption happen in the same loop, so
``asyncio.Queue`` is safe without locks.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from aakaar.interpreter.events import EventRecorder, RecordedEvent

logger = logging.getLogger(__name__)


class EventBroker:
    def __init__(self) -> None:
        self._subs: dict[uuid.UUID, set[asyncio.Queue[dict[str, Any]]]] = {}

    def subscribe(self, run_id: uuid.UUID) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._subs.setdefault(run_id, set()).add(q)
        return q

    def unsubscribe(self, run_id: uuid.UUID, q: asyncio.Queue[dict[str, Any]]) -> None:
        subs = self._subs.get(run_id)
        if subs is not None:
            subs.discard(q)
            if not subs:
                self._subs.pop(run_id, None)

    def publish(self, run_id: uuid.UUID, message: dict[str, Any]) -> None:
        for q in list(self._subs.get(run_id, ())):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning("event broker queue full for run %s; dropping", run_id)


@dataclass
class BroadcastingEventRecorder:
    """EventRecorder that delegates to ``inner`` then publishes to the broker."""

    inner: EventRecorder
    broker: EventBroker

    def record(
        self,
        *,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        node_id: str | None,
        kind: str,
        payload: dict[str, Any] | None = None,
    ) -> RecordedEvent:
        rec = self.inner.record(
            run_id=run_id,
            tenant_id=tenant_id,
            node_id=node_id,
            kind=kind,
            payload=payload,
        )
        try:
            self.broker.publish(
                run_id,
                {
                    "sequence": rec.sequence,
                    "node_id": node_id,
                    "kind": kind,
                    "payload": payload or {},
                    "at": rec.at.isoformat(),
                },
            )
        except Exception:  # pragma: no cover - a broadcast must never break a run
            logger.debug("event broadcast failed for run %s", run_id, exc_info=True)
        return rec


__all__ = ["BroadcastingEventRecorder", "EventBroker"]
