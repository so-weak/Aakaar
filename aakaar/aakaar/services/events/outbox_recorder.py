"""At-least-once, restart-safe run-event fan-out via the durability outbox.

`BroadcastingEventRecorder` (broker.py) publishes each event to live WS
subscribers as a fire-and-forget side effect of recording it — fine while the
process stays up, but an event recorded just before a crash is never delivered,
and there's no record that delivery was pending.

`OutboxEventRecorder` is the durable replacement. It still wraps the canonical
`DbEventRecorder` (which writes the `run_events` row with ``published=False``,
the column default), but instead of publishing inline it hands the freshly
recorded event to a `EventOutbox`, which:

  - dispatches it to the in-process broker (the same `publish` sink), and
  - flips the row to ``published=True`` only AFTER dispatch returns.

A crash between the row insert and the dispatch leaves the row unpublished, and
the startup `EventOutbox.sweep()` replays every still-unpublished row in
``(run_id, sequence)`` order. Delivery is therefore at-least-once: a subscriber
reconnecting after a restart may see an event again, which the UI dedupes on
``(run_id, sequence)``.

This recorder REPLACES `BroadcastingEventRecorder` in the wiring — it must not
sit alongside it, or every event would be published twice on the happy path.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from aakaar.interpreter.durability import EventOutbox
from aakaar.interpreter.events import EventRecorder, RecordedEvent

logger = logging.getLogger(__name__)


@dataclass
class OutboxEventRecorder:
    """EventRecorder that records via ``inner`` then fans out via the outbox."""

    inner: EventRecorder
    outbox: EventOutbox

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
        # Dispatch + mark-published. The outbox swallows publish failures (the
        # row stays unpublished for the next sweep); guard the whole call too so
        # a fan-out problem can never break a run mid-flight.
        try:
            self.outbox.dispatch(
                run_id=run_id,
                sequence=rec.sequence,
                node_id=node_id,
                kind=kind,
                payload=payload or {},
                at=rec.at,
            )
        except Exception:  # pragma: no cover - a broadcast must never break a run
            logger.debug("event outbox dispatch failed for run %s", run_id, exc_info=True)
        return rec


__all__ = ["OutboxEventRecorder"]
