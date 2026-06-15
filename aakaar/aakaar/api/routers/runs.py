"""Run endpoints — start, list, inspect, respond, lifecycle controls.

Edit policy:
  - any tenant user can start a run (read access on any workflow)
  - any tenant user can read run + events (full timeline UI)
  - only the run's started_by user can respond to a human.prompt for that
    run (prevents accidental cross-user responses)
  - pause/resume/cancel require the run's starter or a tenant admin
  - rerun follows the start policy (any tenant user) — it just starts a new
    run pinned to the source run's workflow version and inputs

Pause vs human.prompt: an operator pause holds the run between DAG layers;
a human.prompt holds a single node inside a layer. Resume releases only the
former — a pending prompt must be answered via /runs/{id}/respond (the
precedence rule is documented in `aakaar.interpreter.controls`).

Provenance: the rerun + lifecycle-control surface was designed against the
diverged Aakaar-Ravi fork's equivalent endpoints as a reference for the
feature's shape and edge cases. This implementation is independently written
to this repo's idioms — shared `_create_and_schedule` launch path, an
injected `AuditRecorder`, the `ControlHub`/`RunControlConflict` control model,
and cooperative `RunCancelled` rather than the reference's direct
`task.cancel()`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from aakaar.api.deps import (
    get_audit,
    get_orchestrator,
    get_session,
    require_tenant_user,
)
from aakaar.api.repositories import approvals as approvals_repo
from aakaar.api.repositories import grants as grants_repo
from aakaar.api.repositories import runs as runs_repo
from aakaar.api.repositories import workflows as workflows_repo
from aakaar.api.schemas import (
    ApprovalPendingResponse,
    PendingPromptResponse,
    RunDetailResponse,
    RunEventResponse,
    RunRespondRequest,
    RunResponse,
    RunStartRequest,
)
from aakaar.db.models import ApprovalSubjectType, Run, RunMode, RunStatus, User, UserRole
from aakaar.interpreter import RunOrchestrator
from aakaar.interpreter.controls import RunControlConflict, RunNotActive
from aakaar.interpreter.signals import SignalNotPending
from aakaar.services.audit import AuditRecorder
from aakaar.services.governance import GatedAction, GovernanceService, workflow_is_gated
from aakaar.shared.dag.types import Dag

logger = logging.getLogger(__name__)
router = APIRouter(tags=["runs"])
_governance = GovernanceService()

_TERMINAL_STATUSES = (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED)


def _to_run_response(run: Run) -> RunResponse:
    return RunResponse(
        id=run.id,
        tenant_id=run.tenant_id,
        workflow_id=run.workflow_id,
        workflow_version=run.workflow_version,
        started_by=run.started_by,
        status=run.status,
        mode=run.mode or RunMode.LIVE,
        started_at=run.started_at,
        ended_at=run.ended_at,
        outputs=run.outputs or {},
        error=run.error,
    )


def _get_tenant_run(session: Session, user: User, run_id: uuid.UUID) -> Run:
    assert user.tenant_id is not None
    run = runs_repo.get_run(session, user.tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


def _require_run_controller(run: Run, user: User) -> None:
    """Lifecycle mutations (pause/resume/cancel): starter or tenant admin."""
    if run.started_by != user.id and user.role != UserRole.TENANT_ADMIN:
        logger.info(
            "run control denied run_id=%s started_by=%s caller=%s",
            run.id,
            run.started_by,
            user.id,
        )
        raise HTTPException(
            status_code=403,
            detail="only the run's starter or a tenant admin can control it",
        )


def _snapshot_grants(session: Session, tenant_id: uuid.UUID) -> dict[str, dict[str, object]]:
    """Snapshot enabled grants at schedule time; re-grants mid-run are not
    honored (and a rerun re-snapshots at rerun time)."""
    granted: dict[str, dict[str, object]] = {}
    for g in grants_repo.list_grants(session, tenant_id):
        if g.enabled:
            granted.setdefault(g.capability_ref, {})[g.account_alias] = {
                "vault_ref": g.vault_ref,
                "input_defaults": dict(g.input_defaults or {}),
            }
    return granted


def _create_and_schedule(
    *,
    session: Session,
    orchestrator: RunOrchestrator,
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID,
    version: int,
    dag: Dag,
    started_by: uuid.UUID,
    inputs: dict[str, Any],
    target: str | None,
    mode: str = RunMode.LIVE,
) -> Run:
    """Shared launch path for start_run and rerun_run: persist the run row,
    then hand it to the orchestrator. ``mode`` ('live' | 'dry_run') is stamped
    on the row; the orchestrator reads it back to pick the live or simulation
    execution path."""
    granted_caps = _snapshot_grants(session, tenant_id)
    run = runs_repo.create_run(
        session,
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        workflow_version=version,
        started_by=started_by,
        inputs=inputs,
        mode=mode,
    )
    session.commit()
    logger.info(
        "run created run_id=%s workflow_id=%s version=%s nodes=%d granted_caps=%d",
        run.id,
        workflow_id,
        version,
        len(dag.nodes),
        len(granted_caps),
    )
    orchestrator.schedule(
        run_id=run.id,
        tenant_id=tenant_id,
        dag=dag,
        granted_caps=granted_caps,
        run_target=target,
    )
    logger.debug("run scheduled run_id=%s run_target=%s", run.id, target)
    return run


def perform_run_start(
    session: Session,
    *,
    orchestrator: RunOrchestrator,
    tenant_id: uuid.UUID,
    started_by: uuid.UUID,
    context: dict[str, Any],
) -> Run:
    """Launch a gate-approved run from its snapshotted ``context``.

    Called by the approvals router once a checker approves a ``run_start``
    request. The DAG is re-read from the pinned version (rather than trusting a
    possibly-large snapshot) so a deleted version surfaces as an error to the
    approver. ``started_by`` is the original maker, carried in the snapshot —
    the run is attributed to who requested it, not the approver."""
    workflow_id = uuid.UUID(str(context["workflow_id"]))
    version = int(context["version"])
    wfv = workflows_repo.get_version(session, tenant_id, workflow_id, version)
    if wfv is None:
        raise KeyError(f"workflow version {version} no longer exists")
    dag = Dag.model_validate(wfv.dag)
    target = context.get("run_target")
    mode = str(context.get("mode") or RunMode.LIVE)
    return _create_and_schedule(
        session=session,
        orchestrator=orchestrator,
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        version=version,
        dag=dag,
        started_by=started_by,
        inputs=dict(context.get("inputs") or {}),
        target=str(target) if target is not None else None,
        mode=mode,
    )


@router.post(
    "/workflows/{workflow_id}/runs",
    response_model=RunResponse | ApprovalPendingResponse,
    status_code=201,
)
async def start_run(
    workflow_id: uuid.UUID,
    body: RunStartRequest,
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
    orchestrator: Annotated[RunOrchestrator, Depends(get_orchestrator)],
    audit: Annotated[AuditRecorder, Depends(get_audit)],
    response: Response,
) -> RunResponse | ApprovalPendingResponse:
    assert user.tenant_id is not None
    logger.info(
        "start_run requested workflow_id=%s tenant_id=%s user_id=%s version=%s",
        workflow_id,
        user.tenant_id,
        user.id,
        body.version,
    )
    workflow = workflows_repo.get_workflow(session, user.tenant_id, workflow_id)
    if workflow is None:
        logger.warning("start_run: workflow_id=%s not found in tenant_id=%s", workflow_id, user.tenant_id)
        raise HTTPException(status_code=404, detail="workflow not found")

    target_version = body.version or workflow.latest_version
    wfv = workflows_repo.get_version(session, user.tenant_id, workflow_id, target_version)
    if wfv is None:
        logger.warning(
            "start_run: workflow_id=%s version=%s not found", workflow_id, target_version
        )
        raise HTTPException(status_code=404, detail=f"version {target_version} not found")

    if workflow_is_gated(
        requires_approval=workflow.requires_approval, sensitivity=workflow.sensitivity
    ):
        # Don't launch — snapshot what a launch needs and open a maker-checker
        # gate. subject_ref is the workflow id (the gated resource); the run row
        # is only created once a different user approves.
        action = GatedAction(
            subject_type=ApprovalSubjectType.RUN_START,
            subject_ref=str(workflow_id),
            context={
                "workflow_id": str(workflow_id),
                "workflow_name": workflow.name,
                "version": target_version,
                "sensitivity": workflow.sensitivity,
                "inputs": dict(body.inputs),
                "run_target": body.target,
                "mode": body.mode,
            },
        )
        req = _governance.open_gate(
            session, tenant_id=user.tenant_id, action=action, requested_by=user.id
        )
        session.commit()
        logger.info(
            "run start gated: approval_id=%s workflow_id=%s requested_by=%s",
            req.id,
            workflow_id,
            user.id,
        )
        audit.record(
            action="run.start.gated",
            tenant_id=user.tenant_id,
            actor_id=user.id,
            target_kind="approval_request",
            target_id=str(req.id),
            payload={"workflow_id": str(workflow_id), "version": target_version},
        )
        response.status_code = status.HTTP_202_ACCEPTED
        return ApprovalPendingResponse(approval=approvals_repo.to_response(req))

    dag = Dag.model_validate(wfv.dag)

    run = _create_and_schedule(
        session=session,
        orchestrator=orchestrator,
        tenant_id=user.tenant_id,
        workflow_id=workflow_id,
        version=target_version,
        dag=dag,
        started_by=user.id,
        inputs=body.inputs,
        target=body.target,
        mode=body.mode,
    )
    audit.record(
        action="run.start",
        tenant_id=user.tenant_id,
        actor_id=user.id,
        target_kind="run",
        target_id=str(run.id),
        payload={
            "workflow_id": str(workflow_id),
            "version": target_version,
            "run_target": body.target,
            "mode": body.mode,
        },
    )
    return _to_run_response(run)


@router.get("/runs", response_model=list[RunResponse])
def list_runs(
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
    active: bool = False,
) -> list[RunResponse]:
    """List recent runs for the caller's tenant. Pass `?active=true` to
    restrict to runs in queued/running/paused status — used by the live
    process console."""
    assert user.tenant_id is not None
    return [
        _to_run_response(r)
        for r in runs_repo.list_runs_for_tenant(
            session, user.tenant_id, active_only=active
        )
    ]


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


@router.post("/runs/{run_id}/pause", response_model=RunResponse)
async def pause_run(
    run_id: uuid.UUID,
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
    orchestrator: Annotated[RunOrchestrator, Depends(get_orchestrator)],
    audit: Annotated[AuditRecorder, Depends(get_audit)],
) -> RunResponse:
    """Operator pause: in-flight nodes finish, no new DAG layer starts."""
    run = _get_tenant_run(session, user, run_id)
    _require_run_controller(run, user)
    if run.status in _TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail="run already finished")
    try:
        orchestrator.pause_run(run_id=run_id)
    except RunNotActive:
        raise HTTPException(
            status_code=409, detail="run is not active on this server"
        ) from None
    except RunControlConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    audit.record(
        action="run.pause",
        tenant_id=user.tenant_id,
        actor_id=user.id,
        target_kind="run",
        target_id=str(run_id),
        payload={},
    )
    session.expire(run)  # orchestrator persisted PAUSED in its own session
    return _to_run_response(run)


@router.post("/runs/{run_id}/resume", response_model=RunResponse)
async def resume_run(
    run_id: uuid.UUID,
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
    orchestrator: Annotated[RunOrchestrator, Depends(get_orchestrator)],
    audit: Annotated[AuditRecorder, Depends(get_audit)],
) -> RunResponse:
    """Release an operator pause. A run waiting on a human.prompt is NOT
    operator-paused — answer it via /runs/{run_id}/respond instead."""
    run = _get_tenant_run(session, user, run_id)
    _require_run_controller(run, user)
    if run.status in _TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail="run already finished")
    try:
        orchestrator.resume_run(run_id=run_id)
    except RunNotActive:
        raise HTTPException(
            status_code=409, detail="run is not active on this server"
        ) from None
    except RunControlConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    audit.record(
        action="run.resume",
        tenant_id=user.tenant_id,
        actor_id=user.id,
        target_kind="run",
        target_id=str(run_id),
        payload={},
    )
    session.expire(run)
    return _to_run_response(run)


@router.post("/runs/{run_id}/cancel", response_model=RunResponse)
async def cancel_run(
    run_id: uuid.UUID,
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
    orchestrator: Annotated[RunOrchestrator, Depends(get_orchestrator)],
    audit: Annotated[AuditRecorder, Depends(get_audit)],
) -> RunResponse:
    """Cooperative cancel: in-flight nodes finish (long waits and pending
    prompts are interrupted), then the run unwinds to CANCELLED. The
    response may still show the pre-terminal status; poll the run for the
    final state. Idempotent while the run is still unwinding."""
    run = _get_tenant_run(session, user, run_id)
    _require_run_controller(run, user)
    if run.status in _TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail="run already finished")
    try:
        await orchestrator.cancel_run(run_id=run_id)
    except RunNotActive:
        raise HTTPException(
            status_code=409, detail="run is not active on this server"
        ) from None
    audit.record(
        action="run.cancel",
        tenant_id=user.tenant_id,
        actor_id=user.id,
        target_kind="run",
        target_id=str(run_id),
        payload={},
    )
    session.expire(run)
    return _to_run_response(run)


@router.post("/runs/{run_id}/rerun", response_model=RunResponse, status_code=201)
async def rerun_run(
    run_id: uuid.UUID,
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
    orchestrator: Annotated[RunOrchestrator, Depends(get_orchestrator)],
    audit: Annotated[AuditRecorder, Depends(get_audit)],
) -> RunResponse:
    """Start a NEW run pinned to the source run's workflow version and
    inputs. Only terminal runs can be re-run; capability grants are
    re-snapshotted at rerun time (not copied from the source run).

    The launch-time run target is NOT carried over (we pass target=None, so
    the rerun uses each node's own target): the Run row does not persist the
    launch placement today. A run pinned to a specific agent/pool at start
    will rerun unpinned. See the followup to persist and restore it."""
    assert user.tenant_id is not None
    source = _get_tenant_run(session, user, run_id)
    if source.status not in _TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="run is still active; rerun is only allowed once it has finished",
        )
    wfv = workflows_repo.get_version(
        session, user.tenant_id, source.workflow_id, source.workflow_version
    )
    if wfv is None:
        raise HTTPException(
            status_code=409,
            detail=f"workflow version {source.workflow_version} no longer exists",
        )
    dag = Dag.model_validate(wfv.dag)

    run = _create_and_schedule(
        session=session,
        orchestrator=orchestrator,
        tenant_id=user.tenant_id,
        workflow_id=source.workflow_id,
        version=source.workflow_version,
        dag=dag,
        started_by=user.id,
        inputs=dict(source.inputs or {}),
        target=None,
        # Preserve the source run's mode: a dry-run reruns as a dry-run, so a
        # rerun can never introduce side effects the original simulated away.
        mode=source.mode or RunMode.LIVE,
    )
    audit.record(
        action="run.rerun",
        tenant_id=user.tenant_id,
        actor_id=user.id,
        target_kind="run",
        target_id=str(run.id),
        payload={
            "rerun_of": str(run_id),
            "workflow": {
                "id": str(source.workflow_id),
                "version": source.workflow_version,
            },
            "node_count": len(dag.nodes),
        },
    )
    return _to_run_response(run)


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
        logger.info(
            "run respond denied run_id=%s started_by=%s caller=%s",
            run_id,
            run.started_by,
            user.id,
        )
        raise HTTPException(
            status_code=403,
            detail="only the user who started the run can respond to its prompts",
        )
    logger.info("run respond run_id=%s node_id=%s", run_id, body.node_id)
    try:
        await orchestrator.respond(
            run_id=run_id, node_id=body.node_id, response=body.response
        )
    except SignalNotPending as e:
        logger.warning(
            "run respond no pending prompt run_id=%s node_id=%s", run_id, body.node_id
        )
        raise HTTPException(
            status_code=409, detail=f"no pending prompt for node {body.node_id!r}"
        ) from e
