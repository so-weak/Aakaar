"""Workflow schedule CRUD. Tenant users manage schedules for their tenant's
workflows; the background scheduler launches them."""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from aakaar.api.deps import get_audit, get_session, require_tenant_user
from aakaar.api.repositories import schedules as schedules_repo
from aakaar.api.repositories import workflows as workflows_repo
from aakaar.api.schemas import (
    ScheduleCreateRequest,
    ScheduleResponse,
    ScheduleUpdateRequest,
)
from aakaar.db.models import User
from aakaar.services.audit import AuditRecorder

logger = logging.getLogger(__name__)
router = APIRouter(tags=["schedules"])


def _validate_trigger(cron: str | None, scheduled_at: object) -> None:
    if bool(cron) == bool(scheduled_at):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="exactly one of 'cron' or 'scheduled_at' must be set",
        )


@router.post(
    "/workflows/{workflow_id}/schedules",
    response_model=ScheduleResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_schedule(
    workflow_id: uuid.UUID,
    body: ScheduleCreateRequest,
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
    audit: Annotated[AuditRecorder, Depends(get_audit)],
) -> ScheduleResponse:
    assert user.tenant_id is not None
    _validate_trigger(body.cron, body.scheduled_at)
    if workflows_repo.get_workflow(session, user.tenant_id, workflow_id) is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    sched = schedules_repo.create_schedule(
        session,
        tenant_id=user.tenant_id,
        workflow_id=workflow_id,
        created_by=user.id,
        cron=body.cron,
        scheduled_at=body.scheduled_at,
        inputs=body.inputs,
        executor_type=body.executor_type,
        target=body.target,
    )
    session.commit()
    audit.record(
        action="schedule.create",
        tenant_id=user.tenant_id,
        actor_id=user.id,
        target_kind="schedule",
        target_id=str(sched.id),
        payload={"workflow_id": str(workflow_id), "cron": body.cron},
    )
    return ScheduleResponse.model_validate(sched)


@router.get(
    "/workflows/{workflow_id}/schedules", response_model=list[ScheduleResponse]
)
def list_schedules(
    workflow_id: uuid.UUID,
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
) -> list[ScheduleResponse]:
    assert user.tenant_id is not None
    rows = schedules_repo.list_for_workflow(
        session, tenant_id=user.tenant_id, workflow_id=workflow_id
    )
    return [ScheduleResponse.model_validate(r) for r in rows]


@router.patch("/schedules/{schedule_id}", response_model=ScheduleResponse)
def update_schedule(
    schedule_id: uuid.UUID,
    body: ScheduleUpdateRequest,
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
) -> ScheduleResponse:
    assert user.tenant_id is not None
    sched = schedules_repo.get(
        session, tenant_id=user.tenant_id, schedule_id=schedule_id
    )
    if sched is None:
        raise HTTPException(status_code=404, detail="schedule not found")
    if body.enabled is not None:
        sched.enabled = body.enabled
    if body.cron is not None:
        sched.cron = body.cron
        sched.scheduled_at = None
    if body.scheduled_at is not None:
        sched.scheduled_at = body.scheduled_at
        sched.cron = None
    if body.inputs is not None:
        sched.inputs = body.inputs
    if body.target is not None:
        sched.target = body.target or None
    _validate_trigger(sched.cron, sched.scheduled_at)
    session.commit()
    return ScheduleResponse.model_validate(sched)


@router.delete("/schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_schedule(
    schedule_id: uuid.UUID,
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
    audit: Annotated[AuditRecorder, Depends(get_audit)],
) -> None:
    assert user.tenant_id is not None
    if not schedules_repo.delete(
        session, tenant_id=user.tenant_id, schedule_id=schedule_id
    ):
        raise HTTPException(status_code=404, detail="schedule not found")
    session.commit()
    audit.record(
        action="schedule.delete",
        tenant_id=user.tenant_id,
        actor_id=user.id,
        target_kind="schedule",
        target_id=str(schedule_id),
    )
