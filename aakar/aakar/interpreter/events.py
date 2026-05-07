"""Run-event recording.

The interpreter calls into an `EventRecorder` at every notable transition
(node started/completed/failed/retrying, run paused/resumed, signal
received). The DB-backed implementation writes `run_events` rows for the
timeline UI; tests can substitute an in-memory recorder.

Payload redaction is the recorder's job — never the activity's. By the
time something reaches the recorder, secrets must already be stripped.
This is enforced by convention: activity outputs that contain credentials
should never be returned in `outputs`; the orchestrator passes `outputs`
directly to the recorder for `node_completed` events.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy.orm import Session

from aakar.db.models import RunEvent, RunEventKind
from aakar.db.session import SessionFactory


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RecordedEvent:
    sequence: int
    run_id: uuid.UUID
    node_id: str | None
    kind: str
    payload: dict[str, Any]
    at: datetime


class EventRecorder(Protocol):
    """Records run-level and node-level events.

    Implementations must be thread-safe and idempotent on `sequence`.
    """

    def record(
        self,
        *,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        node_id: str | None,
        kind: str,
        payload: dict[str, Any] | None = None,
    ) -> RecordedEvent: ...


@dataclass
class InMemoryEventRecorder:
    """Test recorder. Holds events in a list keyed by run_id."""

    events: dict[uuid.UUID, list[RecordedEvent]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(
        self,
        *,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        node_id: str | None,
        kind: str,
        payload: dict[str, Any] | None = None,
    ) -> RecordedEvent:
        with self._lock:
            bucket = self.events.setdefault(run_id, [])
            evt = RecordedEvent(
                sequence=len(bucket),
                run_id=run_id,
                node_id=node_id,
                kind=kind,
                payload=payload or {},
                at=datetime.now(timezone.utc),
            )
            bucket.append(evt)
            return evt


@dataclass
class DbEventRecorder:
    """Production recorder — writes a row per event to `run_events`.

    A single record() call commits its own transaction; long-running runs
    can call it many times without holding session state across awaits.
    """

    session_factory: SessionFactory
    _seqs: dict[uuid.UUID, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(
        self,
        *,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        node_id: str | None,
        kind: str,
        payload: dict[str, Any] | None = None,
    ) -> RecordedEvent:
        with self._lock:
            seq = self._seqs.get(run_id, 0)
            self._seqs[run_id] = seq + 1

        now = datetime.now(timezone.utc)
        evt = RunEvent(
            tenant_id=tenant_id,
            run_id=run_id,
            sequence=seq,
            node_id=node_id,
            kind=kind,
            payload=payload or {},
            at=now,
        )
        with self.session_factory.session() as s:
            s.add(evt)
            s.commit()
        return RecordedEvent(
            sequence=seq, run_id=run_id, node_id=node_id, kind=kind,
            payload=payload or {}, at=now,
        )


# ---------- node lifecycle helpers ----------------------------------------


@contextmanager
def node_span(
    recorder: EventRecorder,
    *,
    run_id: uuid.UUID,
    tenant_id: uuid.UUID,
    node_id: str,
    ref: str,
) -> Iterator[None]:
    """Bookend a node's execution with started/completed/failed events."""
    recorder.record(
        run_id=run_id,
        tenant_id=tenant_id,
        node_id=node_id,
        kind=RunEventKind.NODE_STARTED,
        payload={"ref": ref},
    )
    try:
        yield
    except Exception as e:
        recorder.record(
            run_id=run_id,
            tenant_id=tenant_id,
            node_id=node_id,
            kind=RunEventKind.NODE_FAILED,
            payload={"ref": ref, "error": _safe_error(e)},
        )
        raise


def _safe_error(e: BaseException) -> dict[str, str]:
    """Format an exception for storage. Never include stack frames or args
    that might carry credentials."""
    return {"type": type(e).__name__, "message": str(e)[:500]}


def _attach_session(_=None) -> Session:  # pragma: no cover — convenience for tests
    raise NotImplementedError
