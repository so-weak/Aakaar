"""Workflow CRUD + versioning.

Edit policy:
  - any tenant user can read/list/get any workflow in their tenant
  - only the workflow's `created_by` can save a new version (or delete)

DAGs submitted by clients are validated against the registry + tenant grants
before save. Manual DAG saves are uncommon — most edits should flow through
the chat endpoint — but we still validate to prevent corruption.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from aakaar.api.deps import get_audit, get_registry, get_session, require_tenant_user
from aakaar.api.repositories import grants as grants_repo
from aakaar.api.repositories import workflows as workflows_repo
from aakaar.api.schemas import (
    WorkflowCreateRequest,
    WorkflowResponse,
    WorkflowUpdateRequest,
    WorkflowVersionResponse,
)
from aakaar.db.models import User
from aakaar.services.audit import AuditRecorder
from aakaar.shared.dag import ValidationError, validate_dag
from aakaar.shared.dag.types import Dag
from aakaar.shared.registry import Registry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/workflows", tags=["workflows"])


def _validate_dag_for_tenant(
    *, dag: Dag, registry: Registry, granted: set[str]
) -> None:
    try:
        validate_dag(dag, registry=registry, granted_capabilities=granted)
    except ValidationError as e:
        logger.info("DAG validation failed: %s", e)
        raise HTTPException(
            status_code=422,
            detail=f"DAG validation failed: {e}",
        ) from e


def _to_response(workflow) -> WorkflowResponse:
    return WorkflowResponse(
        id=workflow.id,
        tenant_id=workflow.tenant_id,
        created_by=workflow.created_by,
        name=workflow.name,
        description=workflow.description,
        latest_version=workflow.latest_version,
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
    )


def _version_to_response(v) -> WorkflowVersionResponse:
    return WorkflowVersionResponse(
        id=v.id,
        workflow_id=v.workflow_id,
        version=v.version,
        dag=Dag.model_validate(v.dag),
        rationale=v.rationale,
        created_by=v.created_by,
        created_at=v.created_at,
    )


@router.post("", response_model=WorkflowResponse, status_code=status.HTTP_201_CREATED)
def create_workflow(
    body: WorkflowCreateRequest,
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
    registry: Annotated[Registry, Depends(get_registry)],
    audit: Annotated[AuditRecorder, Depends(get_audit)],
) -> WorkflowResponse:
    assert user.tenant_id is not None
    granted = grants_repo.list_granted_refs(session, user.tenant_id)
    _validate_dag_for_tenant(dag=body.dag, registry=registry, granted=granted)

    workflow, _ = workflows_repo.create_workflow(
        session,
        tenant_id=user.tenant_id,
        created_by=user.id,
        name=body.name,
        description=body.description,
        dag=body.dag,
        rationale=body.rationale,
    )
    session.commit()
    logger.info(
        "workflow created id=%s tenant_id=%s name=%r nodes=%d",
        workflow.id,
        user.tenant_id,
        body.name,
        len(body.dag.nodes),
    )
    audit.record(
        action="workflow.create",
        tenant_id=user.tenant_id,
        actor_id=user.id,
        target_kind="workflow",
        target_id=str(workflow.id),
        payload={"name": body.name, "nodes": len(body.dag.nodes)},
    )
    return _to_response(workflow)


@router.get("", response_model=list[WorkflowResponse])
def list_workflows(
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
) -> list[WorkflowResponse]:
    assert user.tenant_id is not None
    return [_to_response(w) for w in workflows_repo.list_workflows(session, user.tenant_id)]


@router.get("/{workflow_id}", response_model=WorkflowResponse)
def get_workflow(
    workflow_id: uuid.UUID,
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
) -> WorkflowResponse:
    assert user.tenant_id is not None
    w = workflows_repo.get_workflow(session, user.tenant_id, workflow_id)
    if w is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    return _to_response(w)


# Order matters: declare `/versions/latest` (literal) BEFORE `/versions/{version}`
# so FastAPI doesn't try to coerce "latest" into the int-typed `version` param.
@router.get("/{workflow_id}/versions/latest", response_model=WorkflowVersionResponse)
def get_latest_version(
    workflow_id: uuid.UUID,
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
) -> WorkflowVersionResponse:
    assert user.tenant_id is not None
    v = workflows_repo.get_latest_version(session, user.tenant_id, workflow_id)
    if v is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    return _version_to_response(v)


@router.get(
    "/{workflow_id}/versions/{version}",
    response_model=WorkflowVersionResponse,
)
def get_workflow_version(
    workflow_id: uuid.UUID,
    version: int,
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
) -> WorkflowVersionResponse:
    assert user.tenant_id is not None
    v = workflows_repo.get_version(session, user.tenant_id, workflow_id, version)
    if v is None:
        raise HTTPException(status_code=404, detail="version not found")
    return _version_to_response(v)


@router.patch("/{workflow_id}", response_model=WorkflowVersionResponse)
def update_workflow(
    workflow_id: uuid.UUID,
    body: WorkflowUpdateRequest,
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
    registry: Annotated[Registry, Depends(get_registry)],
) -> WorkflowVersionResponse:
    assert user.tenant_id is not None
    workflow = workflows_repo.get_workflow(session, user.tenant_id, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    if workflow.created_by != user.id:
        raise HTTPException(
            status_code=403, detail="only the workflow's owner can save new versions"
        )

    granted = grants_repo.list_granted_refs(session, user.tenant_id)
    _validate_dag_for_tenant(dag=body.dag, registry=registry, granted=granted)

    version = workflows_repo.add_version(
        session,
        tenant_id=user.tenant_id,
        workflow_id=workflow_id,
        created_by=user.id,
        dag=body.dag,
        rationale=body.rationale,
    )
    session.commit()
    logger.info(
        "workflow version added workflow_id=%s version=%s nodes=%d",
        workflow_id,
        version.version,
        len(body.dag.nodes),
    )
    return _version_to_response(version)


@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workflow(
    workflow_id: uuid.UUID,
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
    audit: Annotated[AuditRecorder, Depends(get_audit)],
) -> None:
    assert user.tenant_id is not None
    workflow = workflows_repo.get_workflow(session, user.tenant_id, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    if workflow.created_by != user.id:
        raise HTTPException(status_code=403, detail="only the owner can delete")
    workflows_repo.delete_workflow(session, user.tenant_id, workflow_id)
    session.commit()
    logger.info("workflow deleted id=%s tenant_id=%s", workflow_id, user.tenant_id)
    audit.record(
        action="workflow.delete",
        tenant_id=user.tenant_id,
        actor_id=user.id,
        target_kind="workflow",
        target_id=str(workflow_id),
    )
