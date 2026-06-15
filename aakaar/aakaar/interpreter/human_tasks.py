"""Durable, SLA-bounded human-in-the-loop tasks.

The in-process `SignalHub` (signals.py) holds a pending `human.prompt` only in
memory: it coordinates the executor coroutine awaiting a response with the HTTP
endpoint that delivers it. That is enough to *wait*, but nothing about the
prompt survives a restart and nothing enforces a deadline. `HumanTaskStore`
mirrors each live prompt into a `human_tasks` row so:

  - an operator can list outstanding tasks (the SignalHub is opaque);
  - a deadline / escalation timer survives a process restart;
  - a background sweep can flip a task to ``escalated``/``expired`` and record
    a run event when its SLA elapses.

It sits alongside the SignalHub, never replacing it — the executor still awaits
the SignalHub future for the actual response. This store only persists the
durable shadow and the SLA bookkeeping. Like the recorder and the checkpoint
store, every method writes through the existing `SessionFactory` with its own
short transaction and never holds a session across an await (these methods are
sync; the executor calls them inline at prompt open/close).

`(run_id, node_id)` is unique in `human_tasks`, matching the SignalHub's
in-memory "at most one live prompt per node" invariant; ``open`` upserts so a
resumed run that re-opens the same prompt overwrites the stale row rather than
violating the constraint.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from aakaar.db.models import HumanTask, HumanTaskStatus, RunEventKind
from aakaar.db.session import SessionFactory
from aakaar.db.tenancy import system_scope
from aakaar.interpreter.events import EventRecorder

logger = logging.getLogger(__name__)


# OTP responses are one-time secrets; never persist the typed value for an
# `otp` prompt. Other expects ('text'/'confirm') store the answer for audit.
_SECRET_EXPECTS = {"otp"}


def _as_aware(dt: datetime | None) -> datetime | None:
    """Coerce a possibly-naive stored datetime to UTC-aware for comparison.

    SQLite drops the tzinfo on round-trip even for `DateTime(timezone=True)`
    columns, so a value read back is naive though it was written aware. Same
    helper the scheduler uses; treats a naive value as already-UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


@dataclass
class HumanTaskStore:
    """Persists and reconciles the durable shadow of in-flight human prompts.

    `sla_seconds` / `escalation_seconds` set the deadline_at / escalation_at on
    a freshly-opened task. `escalation_seconds` should be <= `sla_seconds`
    (escalate before the hard deadline); both are clamped to the prompt's own
    timeout by the caller so a task never outlives the coroutine waiting on it.
    """

    session_factory: SessionFactory
    recorder: EventRecorder | None = None
    sla_seconds: int = 3600
    escalation_seconds: int = 1800

    def open(
        self,
        *,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        node_id: str,
        message: str,
        expects: str,
        timeout_seconds: int | None = None,
    ) -> None:
        """Upsert a PENDING task for a just-opened prompt with SLA timers.

        Idempotent on `(run_id, node_id)`: a re-opened prompt (resume) reuses
        the existing row, resetting it to PENDING with fresh timers. Never
        raises into the run — a persistence failure leaves the SignalHub flow
        intact, only the durable shadow is missing.
        """
        now = datetime.now(UTC)
        # Don't let the SLA outlive the prompt's own timeout: a task that
        # escalates after the coroutine already gave up is noise.
        sla = self.sla_seconds
        esc = self.escalation_seconds
        if timeout_seconds is not None and timeout_seconds > 0:
            sla = min(sla, timeout_seconds)
            esc = min(esc, sla)
        deadline_at = now + timedelta(seconds=sla)
        escalation_at = now + timedelta(seconds=esc)
        try:
            with self.session_factory.session() as s:
                row = s.scalar(
                    select(HumanTask).where(
                        HumanTask.run_id == run_id, HumanTask.node_id == node_id
                    )
                )
                if row is None:
                    row = HumanTask(
                        tenant_id=tenant_id,
                        run_id=run_id,
                        node_id=node_id,
                        prompt=message[:8000],
                        expects=expects,
                        status=HumanTaskStatus.PENDING,
                        deadline_at=deadline_at,
                        escalation_at=escalation_at,
                        created_at=now,
                    )
                    s.add(row)
                else:
                    row.prompt = message[:8000]
                    row.expects = expects
                    row.status = HumanTaskStatus.PENDING
                    row.deadline_at = deadline_at
                    row.escalation_at = escalation_at
                    row.responded_at = None
                    row.responded_by = None
                    row.response = None
                s.commit()
        except Exception:
            logger.warning(
                "human task open failed run_id=%s node_id=%s; continuing on SignalHub only",
                run_id,
                node_id,
                exc_info=True,
            )

    def resolve(
        self,
        *,
        run_id: uuid.UUID,
        node_id: str,
        response: str,
        responded_by: uuid.UUID | None = None,
    ) -> None:
        """Mark the task RESPONDED when its prompt is answered.

        Only transitions a PENDING/ESCALATED task — a task already terminal
        (expired/cancelled) is left as-is. The response value is stored for
        audit except for `otp` prompts, whose value is a one-time secret and is
        recorded only as its length.
        """
        now = datetime.now(UTC)
        try:
            with self.session_factory.session() as s:
                row = s.scalar(
                    select(HumanTask).where(
                        HumanTask.run_id == run_id, HumanTask.node_id == node_id
                    )
                )
                if row is None:
                    return
                if row.status not in (
                    HumanTaskStatus.PENDING,
                    HumanTaskStatus.ESCALATED,
                ):
                    return
                row.status = HumanTaskStatus.RESPONDED
                row.responded_at = now
                row.responded_by = responded_by
                row.response = (
                    f"<redacted otp len={len(response)}>"
                    if row.expects in _SECRET_EXPECTS
                    else response[:8000]
                )
                s.commit()
        except Exception:
            logger.warning(
                "human task resolve failed run_id=%s node_id=%s", run_id, node_id, exc_info=True
            )

    def cancel(self, *, run_id: uuid.UUID, node_id: str) -> None:
        """Mark a still-live task CANCELLED (the prompt was abandoned).

        Used when a human.prompt unwinds via operator cancel or timeout so the
        durable shadow doesn't linger as a phantom PENDING task forever.
        """
        now = datetime.now(UTC)
        try:
            with self.session_factory.session() as s:
                row = s.scalar(
                    select(HumanTask).where(
                        HumanTask.run_id == run_id, HumanTask.node_id == node_id
                    )
                )
                if row is None:
                    return
                if row.status not in (
                    HumanTaskStatus.PENDING,
                    HumanTaskStatus.ESCALATED,
                ):
                    return
                row.status = HumanTaskStatus.CANCELLED
                row.responded_at = now
                s.commit()
        except Exception:
            logger.warning(
                "human task cancel failed run_id=%s node_id=%s", run_id, node_id, exc_info=True
            )

    def sweep_escalations(self) -> int:
        """Escalate every PENDING task past its `escalation_at`.

        Called periodically from a lifespan task. Flips PENDING -> ESCALATED for
        any task whose escalation deadline has passed (still awaiting a response)
        and records a `run_paused` event noting the escalation so the timeline /
        any notifier can react. Returns the number escalated. Idempotent: a task
        already ESCALATED is skipped, so re-running the sweep won't re-escalate.

        Tasks past `deadline_at` with no response are marked EXPIRED — but only
        the executor's prompt timeout resolves the actual coroutine; this is the
        durable record catching up when the in-process timer is gone (restart).
        """
        now = datetime.now(UTC)
        escalated = 0
        expired = 0
        # Snapshot rows to act on, then emit events outside the read so a slow
        # recorder can't hold the read transaction open. The sweep spans every
        # tenant's tasks — a trusted cross-tenant read, so enter system_scope so
        # RLS (when enforcing on Postgres) doesn't filter it to nothing.
        to_escalate: list[tuple[uuid.UUID, uuid.UUID, str]] = []
        with system_scope(), self.session_factory.session() as s:
            rows = list(
                s.scalars(
                    select(HumanTask).where(
                        HumanTask.status == HumanTaskStatus.PENDING
                    )
                )
            )
            for row in rows:
                deadline = _as_aware(row.deadline_at)
                escalate_at = _as_aware(row.escalation_at)
                if deadline is not None and deadline <= now:
                    row.status = HumanTaskStatus.EXPIRED
                    expired += 1
                    continue
                if escalate_at is not None and escalate_at <= now:
                    row.status = HumanTaskStatus.ESCALATED
                    to_escalate.append((row.run_id, row.tenant_id, row.node_id))
                    escalated += 1
            if escalated or expired:
                s.commit()
        if self.recorder is not None:
            for run_id, tenant_id, node_id in to_escalate:
                try:
                    self.recorder.record(
                        run_id=run_id,
                        tenant_id=tenant_id,
                        node_id=node_id,
                        kind=RunEventKind.RUN_PAUSED,
                        payload={"reason": "human_prompt_escalated"},
                    )
                except Exception:
                    logger.debug(
                        "escalation event record failed run_id=%s node_id=%s",
                        run_id,
                        node_id,
                        exc_info=True,
                    )
        if escalated or expired:
            logger.info(
                "human task sweep: escalated=%d expired=%d", escalated, expired
            )
        return escalated


@dataclass
class HumanTaskEscalator:
    """Background periodic runner for `HumanTaskStore.sweep_escalations`.

    Mirrors the Scheduler's start/stop lifecycle so the app lifespan wires it
    the same way (a `create_task` loop cancelled on shutdown). Inert when no
    human.prompt tasks are outstanding — the sweep is a single indexed query.
    """

    store: HumanTaskStore
    tick_seconds: float = 60.0

    def __post_init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())
        logger.info("human-task escalator started (tick=%.1fs)", self.tick_seconds)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("human-task escalator stopped")

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.store.sweep_escalations()
            except Exception:
                logger.exception("human-task escalation sweep failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick_seconds)
