"""Run endpoints — start, list, inspect, respond.

Edit policy:
  - any tenant user can start a run (read access on any workflow)
  - any tenant user can read run + events (full timeline UI)
  - only the run's started_by user can respond to a human.prompt for that
    run (prevents accidental cross-user responses)
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from aakar.api.deps import (
    get_orchestrator,
    get_session,
    require_tenant_user,
)
from aakar.api.repositories import grants as grants_repo
from aakar.api.repositories import runs as runs_repo
from aakar.api.repositories import workflows as workflows_repo
from aakar.api.schemas import (
    PendingPromptResponse,
    RunDetailResponse,
    RunEventResponse,
    RunResponse,
    RunRespondRequest,
    RunStartRequest,
)
from aakar.db.models import User
from aakar.interpreter import RunOrchestrator
from aakar.interpreter.signals import SignalNotPending
from aakar.shared.dag.types import Dag


router = APIRouter(tags=["runs"])


def _to_run_response(run) -> RunResponse:
    return RunResponse(
        id=run.id,
        tenant_id=run.tenant_id,
        workflow_id=run.workflow_id,
        workflow_version=run.workflow_version,
        started_by=run.started_by,
        status=run.status,
        started_at=run.started_at,
        ended_at=run.ended_at,
        outputs=run.outputs or {},
        error=run.error,
    )


@router.post(
    "/workflows/{workflow_id}/runs",
    response_model=RunResponse,
    status_code=201,
)
async def start_run(
    workflow_id: uuid.UUID,
    body: RunStartRequest,
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
    orchestrator: Annotated[RunOrchestrator, Depends(get_orchestrator)],
) -> RunResponse:
    assert user.tenant_id is not None
    workflow = workflows_repo.get_workflow(session, user.tenant_id, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="workflow not found")

    target_version = body.version or workflow.latest_version
    wfv = workflows_repo.get_version(session, user.tenant_id, workflow_id, target_version)
    if wfv is None:
        raise HTTPException(status_code=404, detail=f"version {target_version} not found")

    dag = Dag.model_validate(wfv.dag)

    # Collect grants once at start; re-grants mid-run are not honored.
    granted_caps: dict[str, dict[str, object]] = {}
    for g in grants_repo.list_grants(session, user.tenant_id):
        if g.enabled:
            granted_caps.setdefault(g.capability_ref, {})[g.account_alias] = {
                "vault_ref": g.vault_ref,
                "input_defaults": dict(g.input_defaults or {}),
            }

    run = runs_repo.create_run(
        session,
        tenant_id=user.tenant_id,
        workflow_id=workflow_id,
        workflow_version=target_version,
        started_by=user.id,
        inputs=body.inputs,
    )
    session.commit()

    orchestrator.schedule(
        run_id=run.id,
        tenant_id=user.tenant_id,
        dag=dag,
        granted_caps=granted_caps,
    )
    return _to_run_response(run)


@router.get("/runs", response_model=list[RunResponse])
def list_runs(
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
) -> list[RunResponse]:
    assert user.tenant_id is not None
    return [_to_run_response(r) for r in runs_repo.list_runs_for_tenant(session, user.tenant_id)]


@router.get("/runs/{run_id}", response_model=RunDetailResponse)
def get_run(
    run_id: uuid.UUID,
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
    orchestrator: Annotated[RunOrchestrator, Depends(get_orchestrator)],
) -> RunDetailResponse:
    assert user.tenant_id is not None
    run = runs_repo.get_run(session, user.tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    events = [
        RunEventResponse(
            sequence=e.sequence,
            node_id=e.node_id,
            kind=e.kind,
            payload=e.payload or {},
            at=e.at,
        )
        for e in runs_repo.list_events(session, user.tenant_id, run_id)
    ]
    pending = [
        PendingPromptResponse(
            node_id=p.node_id, message=p.message, expects=p.expects
        )
        for p in orchestrator.signals.list_pending(run_id)
    ]
    return RunDetailResponse(
        run=_to_run_response(run), events=events, pending_prompts=pending
    )


@router.post("/runs/{run_id}/respond", status_code=204)
async def respond_to_run(
    run_id: uuid.UUID,
    body: RunRespondRequest,
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
    orchestrator: Annotated[RunOrchestrator, Depends(get_orchestrator)],
) -> None:
    assert user.tenant_id is not None
    run = runs_repo.get_run(session, user.tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.started_by != user.id:
        raise HTTPException(
            status_code=403,
            detail="only the user who started the run can respond to its prompts",
        )
    try:
        await orchestrator.respond(
            run_id=run_id, node_id=body.node_id, response=body.response
        )
    except SignalNotPending as e:
        raise HTTPException(
            status_code=409, detail=f"no pending prompt for node {body.node_id!r}"
        ) from e
