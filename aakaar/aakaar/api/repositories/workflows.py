"""Workflow + version repository.

Workflows are versioned; saving a new DAG creates a new `WorkflowVersion`
row and bumps `Workflow.latest_version`. Older versions are immutable so
in-flight runs always have a stable DAG to walk.

Edit authority: only the workflow's `created_by` can save a new version.
All tenant users can read and run any version. Authorization is enforced
in the router; the repository surfaces the data.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from aakaar.db.models import Workflow, WorkflowSensitivity, WorkflowVersion
from aakaar.shared.dag.types import Dag


class WorkflowNotFound(LookupError):
    pass


def create_workflow(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    created_by: uuid.UUID,
    name: str,
    description: str,
    dag: Dag,
    rationale: str = "",
    requires_approval: bool = False,
    sensitivity: str = WorkflowSensitivity.NORMAL,
) -> tuple[Workflow, WorkflowVersion]:
    workflow = Workflow(
        tenant_id=tenant_id,
        created_by=created_by,
        name=name,
        description=description,
        latest_version=1,
        requires_approval=requires_approval,
        sensitivity=sensitivity,
    )
    session.add(workflow)
    session.flush()

    version = WorkflowVersion(
        tenant_id=tenant_id,
        workflow_id=workflow.id,
        version=1,
        dag=dag.model_dump(by_alias=True),
        rationale=rationale,
        created_by=created_by,
        requires_approval=requires_approval,
        sensitivity=sensitivity,
    )
    session.add(version)
    session.flush()
    return workflow, version


def add_version(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID,
    created_by: uuid.UUID,
    dag: Dag,
    rationale: str = "",
) -> WorkflowVersion:
    workflow = session.get(Workflow, workflow_id)
    if workflow is None or workflow.tenant_id != tenant_id:
        raise WorkflowNotFound(str(workflow_id))
    next_version = workflow.latest_version + 1
    # Freeze the workflow's current gate flags onto the version so a later
    # change to the workflow can't retroactively un-gate an approved version.
    version = WorkflowVersion(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        version=next_version,
        dag=dag.model_dump(by_alias=True),
        rationale=rationale,
        created_by=created_by,
        requires_approval=workflow.requires_approval,
        sensitivity=workflow.sensitivity,
    )
    session.add(version)
    workflow.latest_version = next_version
    session.flush()
    return version


def list_workflows(session: Session, tenant_id: uuid.UUID) -> list[Workflow]:
    return list(
        session.scalars(
            select(Workflow)
            .where(Workflow.tenant_id == tenant_id)
            .order_by(Workflow.updated_at.desc())
        )
    )


def get_workflow(
    session: Session, tenant_id: uuid.UUID, workflow_id: uuid.UUID
) -> Workflow | None:
    workflow = session.get(Workflow, workflow_id)
    if workflow is None or workflow.tenant_id != tenant_id:
        return None
    return workflow


def get_version(
    session: Session,
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID,
    version: int,
) -> WorkflowVersion | None:
    row = session.scalars(
        select(WorkflowVersion).where(
            WorkflowVersion.tenant_id == tenant_id,
            WorkflowVersion.workflow_id == workflow_id,
            WorkflowVersion.version == version,
        )
    ).first()
    return row


def get_latest_version(
    session: Session, tenant_id: uuid.UUID, workflow_id: uuid.UUID
) -> WorkflowVersion | None:
    workflow = get_workflow(session, tenant_id, workflow_id)
    if workflow is None:
        return None
    return get_version(session, tenant_id, workflow_id, workflow.latest_version)


def latest_meaningful_rationale(
    session: Session, tenant_id: uuid.UUID, workflow_id: uuid.UUID
) -> str:
    """Newest non-placeholder version rationale for a workflow.

    Used to seed the chat composer when a workflow is opened for refinement
    but has no prior chat instruction to echo. Skips the "visual dag edit"
    placeholder written by hand-edits so the seed is actually descriptive.
    """
    rationales = session.scalars(
        select(WorkflowVersion.rationale)
        .where(
            WorkflowVersion.tenant_id == tenant_id,
            WorkflowVersion.workflow_id == workflow_id,
        )
        .order_by(WorkflowVersion.version.desc())
    )
    placeholders = {"visual dag edit"}
    for rationale in rationales:
        text = (rationale or "").strip()
        if text and text.lower() not in placeholders:
            return text
    return ""


def delete_workflow(
    session: Session, tenant_id: uuid.UUID, workflow_id: uuid.UUID
) -> bool:
    workflow = get_workflow(session, tenant_id, workflow_id)
    if workflow is None:
        return False
    session.delete(workflow)
    session.flush()
    return True
