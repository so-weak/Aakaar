"""Persistence for workflow schedules. Tenant-scoped except `list_enabled`,
which the background scheduler uses to sweep every tenant."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aakaar.db.models import WorkflowSchedule


def create_schedule(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID,
    created_by: uuid.UUID,
    cron: str | None,
    scheduled_at: datetime | None,
    inputs: dict[str, Any] | None,
    executor_type: str = "local",
    target: str | None = None,
) -> WorkflowSchedule:
    sched = WorkflowSchedule(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        created_by=created_by,
        cron=cron,
        scheduled_at=scheduled_at,
        inputs=inputs or {},
        executor_type=executor_type,
        target=target,
        enabled=True,
    )
    session.add(sched)
    session.flush()
    return sched


def list_for_workflow(
    session: Session, *, tenant_id: uuid.UUID, workflow_id: uuid.UUID
) -> list[WorkflowSchedule]:
    stmt = (
        select(WorkflowSchedule)
        .where(WorkflowSchedule.tenant_id == tenant_id)
        .where(WorkflowSchedule.workflow_id == workflow_id)
        .order_by(WorkflowSchedule.created_at.desc())
    )
    return list(session.scalars(stmt))


def get(
    session: Session, *, tenant_id: uuid.UUID, schedule_id: uuid.UUID
) -> WorkflowSchedule | None:
    sched = session.get(WorkflowSchedule, schedule_id)
    if sched is None or sched.tenant_id != tenant_id:
        return None
    return sched


def delete(session: Session, *, tenant_id: uuid.UUID, schedule_id: uuid.UUID) -> bool:
    sched = get(session, tenant_id=tenant_id, schedule_id=schedule_id)
    if sched is None:
        return False
    session.delete(sched)
    return True


def list_enabled(session: Session) -> list[WorkflowSchedule]:
    """All enabled schedules across tenants — used only by the scheduler."""
    stmt = select(WorkflowSchedule).where(WorkflowSchedule.enabled.is_(True))
    return list(session.scalars(stmt))


def mark_triggered(
    session: Session, *, schedule_id: uuid.UUID, when: datetime, disable: bool = False
) -> None:
    sched = session.get(WorkflowSchedule, schedule_id)
    if sched is None:
        return
    sched.last_triggered_at = when
    if disable:
        sched.enabled = False
