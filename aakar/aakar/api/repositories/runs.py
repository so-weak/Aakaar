"""Run repository — read/write the `runs` table.

Run rows are the high-level status surface (queued → running → succeeded
| failed | paused). Detailed timeline events live in `run_events` and are
written by the EventRecorder, not by this module.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from aakar.db.models import Run, RunEvent, RunStatus


def create_run(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID,
    workflow_version: int,
    started_by: uuid.UUID,
    inputs: dict | None = None,
) -> Run:
    run = Run(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        workflow_version=workflow_version,
        started_by=started_by,
        status=RunStatus.QUEUED,
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
    outputs: dict | None = None,
    error: dict | None = None,
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
        run.ended_at = datetime.now(timezone.utc)
    session.flush()
    return run


def get_run(session: Session, tenant_id: uuid.UUID, run_id: uuid.UUID) -> Run | None:
    run = session.get(Run, run_id)
    if run is None or run.tenant_id != tenant_id:
        return None
    return run


def list_runs_for_tenant(session: Session, tenant_id: uuid.UUID, limit: int = 100) -> list[Run]:
    return list(
        session.scalars(
            select(Run)
            .where(Run.tenant_id == tenant_id)
            .order_by(Run.started_at.desc())
            .limit(limit)
        )
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
