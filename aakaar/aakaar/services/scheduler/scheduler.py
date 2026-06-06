"""Background workflow scheduler.

A single in-process asyncio task (no Redis, no external cron) that wakes every
``tick_seconds``, finds enabled schedules that are due, and launches a run for
each via the orchestrator — the same path the manual "start run" endpoint uses.

Due-ness:
- one-off (``scheduled_at`` set): due once when now >= scheduled_at and it has
  not been triggered yet; disabled after firing.
- recurring (``cron`` set): due when the next cron fire after the last trigger
  (or creation) is <= now. Fires at most once per tick — a long outage results
  in a single catch-up run, not a backfill storm.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aakaar.api.repositories import grants as grants_repo
from aakaar.api.repositories import runs as runs_repo
from aakaar.api.repositories import schedules as schedules_repo
from aakaar.api.repositories import workflows as workflows_repo
from aakaar.db.session import SessionFactory
from aakaar.interpreter import RunOrchestrator
from aakaar.shared.dag.types import Dag

logger = logging.getLogger(__name__)


def _as_aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as UTC for comparison."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


@dataclass(frozen=True)
class _DueSchedule:
    id: uuid.UUID
    tenant_id: uuid.UUID
    workflow_id: uuid.UUID
    created_by: uuid.UUID | None
    inputs: dict[str, Any]
    is_oneoff: bool
    target: str | None = None


class Scheduler:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        orchestrator: RunOrchestrator,
        tick_seconds: float = 5.0,
    ) -> None:
        self._sf = session_factory
        self._orch = orchestrator
        self._tick = tick_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())
        logger.info("scheduler started (tick=%.1fs)", self._tick)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("scheduler stopped")

    # --- internals ---------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick_once()
            except Exception:
                logger.exception("scheduler tick failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick)

    async def tick_once(self) -> int:
        """Find due schedules and launch them. Returns the number launched
        (also callable directly from tests)."""
        now = datetime.now(UTC)
        due: list[_DueSchedule] = []
        with self._sf.session() as s:
            for sched in schedules_repo.list_enabled(s):
                if self._is_due(sched, now):
                    due.append(
                        _DueSchedule(
                            id=sched.id,
                            tenant_id=sched.tenant_id,
                            workflow_id=sched.workflow_id,
                            created_by=sched.created_by,
                            inputs=dict(sched.inputs or {}),
                            is_oneoff=sched.scheduled_at is not None,
                            target=sched.target,
                        )
                    )
        launched = 0
        for snap in due:
            if await self._trigger(snap, now):
                launched += 1
        if launched:
            logger.info("scheduler launched %d run(s)", launched)
        return launched

    @staticmethod
    def _is_due(sched: Any, now: datetime) -> bool:
        scheduled_at = _as_aware(sched.scheduled_at)
        if scheduled_at is not None:
            return sched.last_triggered_at is None and scheduled_at <= now
        if sched.cron:
            from croniter import croniter

            base = _as_aware(sched.last_triggered_at) or _as_aware(sched.created_at) or now
            try:
                nxt = croniter(sched.cron, base).get_next(datetime)
            except (ValueError, KeyError):
                logger.warning("invalid cron %r on schedule %s", sched.cron, sched.id)
                return False
            return _as_aware(nxt) <= now
        return False

    async def _trigger(self, snap: _DueSchedule, now: datetime) -> bool:
        dag: Dag | None = None
        granted_caps: dict[str, dict[str, object]] = {}
        run_id: uuid.UUID | None = None
        with self._sf.session() as s:
            wf = workflows_repo.get_workflow(s, snap.tenant_id, snap.workflow_id)
            if wf is None or snap.created_by is None:
                # Workflow gone or no actor to attribute the run to — retire it.
                schedules_repo.mark_triggered(
                    s, schedule_id=snap.id, when=now, disable=True
                )
                s.commit()
                return False
            wfv = workflows_repo.get_version(
                s, snap.tenant_id, snap.workflow_id, wf.latest_version
            )
            if wfv is None:
                schedules_repo.mark_triggered(
                    s, schedule_id=snap.id, when=now, disable=snap.is_oneoff
                )
                s.commit()
                return False
            dag = Dag.model_validate(wfv.dag)
            for g in grants_repo.list_grants(s, snap.tenant_id):
                if g.enabled:
                    granted_caps.setdefault(g.capability_ref, {})[g.account_alias] = {
                        "vault_ref": g.vault_ref,
                        "input_defaults": dict(g.input_defaults or {}),
                    }
            run = runs_repo.create_run(
                s,
                tenant_id=snap.tenant_id,
                workflow_id=snap.workflow_id,
                workflow_version=wf.latest_version,
                started_by=snap.created_by,
                inputs=snap.inputs,
            )
            schedules_repo.mark_triggered(
                s, schedule_id=snap.id, when=now, disable=snap.is_oneoff
            )
            s.commit()
            run_id = run.id

        if dag is not None and run_id is not None:
            self._orch.schedule(
                run_id=run_id,
                tenant_id=snap.tenant_id,
                dag=dag,
                granted_caps=granted_caps,
                run_target=snap.target,
            )
            logger.info(
                "scheduler triggered schedule=%s -> run=%s tenant=%s",
                snap.id,
                run_id,
                snap.tenant_id,
            )
            return True
        return False
