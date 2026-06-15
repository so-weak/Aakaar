"""Run repository — read/write the `runs` table.

Run rows are the high-level status surface (queued → running → succeeded
| failed | paused). Detailed timeline events live in `run_events` and are
written by the EventRecorder, not by this module.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aakaar.db.models import Run, RunEvent, RunMode, RunStatus


def create_run(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID,
    workflow_version: int,
    started_by: uuid.UUID,
    inputs: dict[str, Any] | None = None,
    mode: str = RunMode.LIVE,
) -> Run:
    run = Run(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        workflow_version=workflow_version,
        started_by=started_by,
        status=RunStatus.QUEUED,
        mode=mode,
        inputs=inputs or {},
        outputs={},
    )
    session.add(run)
    session.flush()
    return run


def update_status(
    session: Session,
    *,
    run_id: uuid.UUID,
    status: str,
    outputs: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    end: bool = False,
) -> Run | None:
    run = session.get(Run, run_id)
    if run is None:
        return None
    run.status = status
    if outputs is not None:
        run.outputs = outputs
    if error is not None:
        run.error = error
    if end:
        run.ended_at = datetime.now(UTC)
    session.flush()
    return run


def get_run(session: Session, tenant_id: uuid.UUID, run_id: uuid.UUID) -> Run | None:
    run = session.get(Run, run_id)
    if run is None or run.tenant_id != tenant_id:
        return None
    return run


_ACTIVE_STATUSES: tuple[str, ...] = (
    RunStatus.QUEUED,
    RunStatus.RUNNING,
    RunStatus.PAUSED,
)


def list_runs_for_tenant(
    session: Session,
    tenant_id: uuid.UUID,
    limit: int = 100,
    *,
    active_only: bool = False,
) -> list[Run]:
    stmt = select(Run).where(Run.tenant_id == tenant_id)
    if active_only:
        stmt = stmt.where(Run.status.in_(_ACTIVE_STATUSES))
    return list(
        session.scalars(stmt.order_by(Run.started_at.desc()).limit(limit))
    )


def list_all_runs(
    session: Session, limit: int = 200, *, active_only: bool = False
) -> list[Run]:
    """Cross-tenant run list. Superuser-only at the API layer."""
    stmt = select(Run)
    if active_only:
        stmt = stmt.where(Run.status.in_(_ACTIVE_STATUSES))
    return list(
        session.scalars(stmt.order_by(Run.started_at.desc()).limit(limit))
    )


def list_events(
    session: Session, tenant_id: uuid.UUID, run_id: uuid.UUID
) -> list[RunEvent]:
    return list(
        session.scalars(
            select(RunEvent)
            .where(RunEvent.tenant_id == tenant_id, RunEvent.run_id == run_id)
            .order_by(RunEvent.sequence)
        )
    )
