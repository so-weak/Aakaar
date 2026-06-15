"""Superuser-only endpoints: tenant CRUD + initial admin creation + cross-tenant
visibility (all users, all grants) + superuser-issued grants."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select as _select
from sqlalchemy.orm import Session

from aakaar.api.deps import (
    get_capability_index,
    get_orchestrator,
    get_session,
    get_vault,
    require_superuser,
)
from aakaar.api.repositories import grants as grants_repo
from aakaar.api.repositories import runs as runs_repo
from aakaar.api.repositories import tenants as tenants_repo
from aakaar.api.repositories import users as users_repo
from aakaar.api.routers.stats import _build_dashboard
from aakaar.api.schemas import (
    DashboardStatsResponse,
    GrantCreateRequest,
    GrantResponse,
    GrantUpdateRequest,
    PendingPromptResponse,
    RunDetailResponse,
    RunEventResponse,
    RunResponse,
    TenantCreateRequest,
    TenantResponse,
    UserResponse,
    WorkflowResponse,
    WorkflowVersionResponse,
)
from aakaar.db.models import (
    Run,
    RunEvent,
    RunMode,
    User,
    UserRole,
    Workflow,
    WorkflowVersion,
)
from aakaar.interpreter import RunOrchestrator
from aakaar.planner import CapabilityIndex
from aakaar.shared.dag.types import Dag
from aakaar.vault import Vault

router = APIRouter(
    prefix="/superuser",
    tags=["superuser"],
    dependencies=[Depends(require_superuser)],
)


@router.post("/tenants", response_model=TenantResponse, status_code=status.HTTP_201_CREATED)
def create_tenant(
    body: TenantCreateRequest,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[User, Depends(require_superuser)],
) -> TenantResponse:
    try:
        tenant = tenants_repo.create_tenant(session, slug=body.slug, name=body.name)
    except tenants_repo.TenantSlugTaken as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"slug taken: {e}") from e

    try:
        users_repo.create_user(
            session,
            tenant_id=tenant.id,
            email=body.admin_email,
            password=body.admin_password,
            role=UserRole.TENANT_ADMIN,
        )
    except users_repo.EmailTaken as e:
        # Roll back the partial creation rather than leaving an orphan tenant.
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"admin email taken: {e}"
        ) from e

    session.commit()
    return TenantResponse(
        id=tenant.id,
        slug=tenant.slug,
        name=tenant.name,
        status=tenant.status,
        created_at=tenant.created_at,
    )


@router.get("/tenants", response_model=list[TenantResponse])
def list_tenants(
    session: Annotated[Session, Depends(get_session)],
) -> list[TenantResponse]:
    return [
        TenantResponse(
            id=t.id, slug=t.slug, name=t.name, status=t.status, created_at=t.created_at
        )
        for t in tenants_repo.list_tenants(session)
    ]


@router.post("/tenants/{tenant_id}/suspend", response_model=TenantResponse)
def suspend_tenant(
    tenant_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
) -> TenantResponse:
    tenant = tenants_repo.suspend_tenant(session, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    session.commit()
    return TenantResponse(
        id=tenant.id, slug=tenant.slug, name=tenant.name, status=tenant.status, created_at=tenant.created_at
    )


@router.get("/tenants/{tenant_id}/users", response_model=list[UserResponse])
def list_tenant_users(
    tenant_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
) -> list[UserResponse]:
    return [
        UserResponse(
            id=u.id,
            tenant_id=u.tenant_id,
            email=u.email,
            role=u.role,
            status=u.status,
            created_at=u.created_at,
        )
        for u in users_repo.list_users_for_tenant(session, tenant_id)
    ]


@router.get("/stats/dashboard", response_model=DashboardStatsResponse)
def get_global_dashboard(
    session: Annotated[Session, Depends(get_session)],
) -> DashboardStatsResponse:
    """Cross-tenant dashboard with per-tenant breakdown."""
    return _build_dashboard(
        session,
        scope="global",
        tenant_id=None,
        user_id=None,
        include_per_tenant=True,
    )


@router.get("/runs/{run_id}", response_model=RunDetailResponse)
def get_any_run(
    run_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    orchestrator: Annotated[RunOrchestrator, Depends(get_orchestrator)],
) -> RunDetailResponse:
    """Cross-tenant run detail. Mirrors GET /runs/{id} for tenant users
    but skips the tenant filter so superusers can drill into any run
    from the operator console."""
    run = session.get(Run, run_id)
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
        for e in session.scalars(
            _select(RunEvent)
            .where(RunEvent.run_id == run_id)
            .order_by(RunEvent.sequence)
        )
    ]
    pending = [
        PendingPromptResponse(
            node_id=p.node_id, message=p.message, expects=p.expects
        )
        for p in orchestrator.signals.list_pending(run_id)
    ]
    return RunDetailResponse(
        run=RunResponse(
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
        ),
        events=events,
        pending_prompts=pending,
    )


@router.get("/workflows/{workflow_id}", response_model=WorkflowResponse)
def get_any_workflow(
    workflow_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
) -> WorkflowResponse:
    """Cross-tenant workflow lookup. Used by the operator console to
    show workflow names on tiles for runs in any tenant."""
    wf = session.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    return WorkflowResponse(
        id=wf.id,
        tenant_id=wf.tenant_id,
        created_by=wf.created_by,
        name=wf.name,
        description=wf.description,
        latest_version=wf.latest_version,
        created_at=wf.created_at,
        updated_at=wf.updated_at,
    )


@router.get(
    "/workflows/{workflow_id}/versions/{version}",
    response_model=WorkflowVersionResponse,
)
def get_any_workflow_version(
    workflow_id: uuid.UUID,
    version: int,
    session: Annotated[Session, Depends(get_session)],
) -> WorkflowVersionResponse:
    """Cross-tenant workflow-version lookup. Used by the live tile grid
    so the LiveDagViewer can render the DAG for a run in any tenant."""
    row = session.scalars(
        _select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == workflow_id,
            WorkflowVersion.version == version,
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="version not found")
    return WorkflowVersionResponse(
        id=row.id,
        workflow_id=row.workflow_id,
        version=row.version,
        dag=Dag.model_validate(row.dag),
        rationale=row.rationale,
        created_by=row.created_by,
        created_at=row.created_at,
    )


@router.get("/runs", response_model=list[RunResponse])
def list_all_runs(
    session: Annotated[Session, Depends(get_session)],
    active: bool = False,
) -> list[RunResponse]:
    """Cross-tenant run list for the operator console. `?active=true`
    restricts to queued/running/paused runs — the only ones that need
    a live tile."""
    return [
        RunResponse(
            id=r.id,
            tenant_id=r.tenant_id,
            workflow_id=r.workflow_id,
            workflow_version=r.workflow_version,
            started_by=r.started_by,
            status=r.status,
            mode=r.mode or RunMode.LIVE,
            started_at=r.started_at,
            ended_at=r.ended_at,
            outputs=r.outputs or {},
            error=r.error,
        )
        for r in runs_repo.list_all_runs(session, active_only=active)
    ]


@router.get("/users", response_model=list[UserResponse])
def list_all_users(
    session: Annotated[Session, Depends(get_session)],
) -> list[UserResponse]:
    """All users across all tenants (and the superuser itself, who has tenant_id=None)."""
    return [
        UserResponse(
            id=u.id,
            tenant_id=u.tenant_id,
            email=u.email,
            role=u.role,
            status=u.status,
            created_at=u.created_at,
        )
        for u in users_repo.list_all_users(session)
    ]


@router.get("/tenants/{tenant_id}/grants", response_model=list[GrantResponse])
def list_tenant_grants(
    tenant_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    vault: Annotated[Vault, Depends(get_vault)],
) -> list[GrantResponse]:
    if tenants_repo.get_tenant(session, tenant_id) is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    out: list[GrantResponse] = []
    for g in grants_repo.list_grants(session, tenant_id):
        try:
            entry = vault.describe(str(tenant_id), g.vault_ref)
            secret_names = list(entry.secret_names)
        except Exception:
            secret_names = []
        out.append(
            GrantResponse(
                id=g.id,
                capability_ref=g.capability_ref,
                account_alias=g.account_alias,
                secret_names=secret_names,
                input_defaults=g.input_defaults,
                enabled=g.enabled,
                created_at=g.created_at,
            )
        )
    return out


@router.post(
    "/tenants/{tenant_id}/grants",
    response_model=GrantResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_tenant_grant(
    tenant_id: uuid.UUID,
    body: GrantCreateRequest,
    superuser: Annotated[User, Depends(require_superuser)],
    session: Annotated[Session, Depends(get_session)],
    vault: Annotated[Vault, Depends(get_vault)],
    capability_index: Annotated[CapabilityIndex, Depends(get_capability_index)],
) -> GrantResponse:
    """Issue a grant on behalf of any tenant. Mirrors POST /admin/grants but
    bypasses the tenant_admin requirement so a superuser can bootstrap or
    repair grants without logging in as the tenant."""
    if tenants_repo.get_tenant(session, tenant_id) is None:
        raise HTTPException(status_code=404, detail="tenant not found")

    defn = capability_index.registry.get(body.capability_ref)
    if defn is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown capability ref: {body.capability_ref}",
        )

    declared = {s.name for s in getattr(defn, "secrets", ())}
    supplied = set(body.secrets.keys())
    if declared and supplied != declared:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"secret names mismatch: capability declares {sorted(declared)}, "
                f"got {sorted(supplied)}"
            ),
        )
    if not declared and supplied:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"capability declares no secrets, got {sorted(supplied)}",
        )

    try:
        grant = grants_repo.create_grant(
            session,
            vault,
            tenant_id=tenant_id,
            created_by=superuser.id,
            capability_ref=body.capability_ref,
            account_alias=body.account_alias,
            secrets=body.secrets,
            input_defaults=body.input_defaults,
        )
    except grants_repo.GrantConflict as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e

    session.commit()

    granted = grants_repo.list_granted_refs(session, tenant_id)
    capability_index.reindex_for_tenant(str(tenant_id), granted)

    return GrantResponse(
        id=grant.id,
        capability_ref=grant.capability_ref,
        account_alias=grant.account_alias,
        secret_names=sorted(body.secrets.keys()),
        input_defaults=grant.input_defaults,
        enabled=grant.enabled,
        created_at=grant.created_at,
    )


@router.patch(
    "/tenants/{tenant_id}/grants/{grant_id}",
    response_model=GrantResponse,
)
def update_tenant_grant(
    tenant_id: uuid.UUID,
    grant_id: uuid.UUID,
    body: GrantUpdateRequest,
    session: Annotated[Session, Depends(get_session)],
    vault: Annotated[Vault, Depends(get_vault)],
    capability_index: Annotated[CapabilityIndex, Depends(get_capability_index)],
) -> GrantResponse:
    """Mirror of PATCH /admin/grants/{id} for any tenant. Same validation
    rules — secret names, when supplied, must match the capability's
    declaration."""
    if tenants_repo.get_tenant(session, tenant_id) is None:
        raise HTTPException(status_code=404, detail="tenant not found")

    grant = grants_repo.get_grant(session, tenant_id=tenant_id, grant_id=grant_id)
    if grant is None:
        raise HTTPException(status_code=404, detail="grant not found")

    if all(
        v is None for v in (body.account_alias, body.secrets, body.input_defaults, body.enabled)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="supply at least one of `account_alias`, `secrets`, `input_defaults`, or `enabled`",
        )

    if body.secrets:
        defn = capability_index.registry.get(grant.capability_ref)
        if defn is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"capability {grant.capability_ref} no longer in registry",
            )
        declared = {s.name for s in getattr(defn, "secrets", ())}
        supplied = set(body.secrets.keys())
        if declared != supplied:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"secret names mismatch: capability declares {sorted(declared)}, "
                    f"got {sorted(supplied)}"
                ),
            )

    try:
        updated = grants_repo.update_grant(
            session,
            vault,
            tenant_id=tenant_id,
            grant_id=grant_id,
            account_alias=body.account_alias,
            secrets=body.secrets,
            input_defaults=body.input_defaults,
            enabled=body.enabled,
        )
    except grants_repo.GrantConflict as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e

    if updated is None:
        raise HTTPException(status_code=404, detail="grant not found")

    session.commit()

    if body.enabled is not None:
        granted = grants_repo.list_granted_refs(session, tenant_id)
        capability_index.reindex_for_tenant(str(tenant_id), granted)

    try:
        entry = vault.describe(str(tenant_id), updated.vault_ref)
        secret_names = list(entry.secret_names)
    except Exception:
        secret_names = []

    return GrantResponse(
        id=updated.id,
        capability_ref=updated.capability_ref,
        account_alias=updated.account_alias,
        secret_names=secret_names,
        input_defaults=updated.input_defaults,
        enabled=updated.enabled,
        created_at=updated.created_at,
    )


@router.delete(
    "/tenants/{tenant_id}/grants/{grant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_tenant_grant(
    tenant_id: uuid.UUID,
    grant_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    vault: Annotated[Vault, Depends(get_vault)],
    capability_index: Annotated[CapabilityIndex, Depends(get_capability_index)],
) -> None:
    if tenants_repo.get_tenant(session, tenant_id) is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    deleted = grants_repo.delete_grant(
        session, vault, tenant_id=tenant_id, grant_id=grant_id
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="grant not found")
    session.commit()
    granted = grants_repo.list_granted_refs(session, tenant_id)
    capability_index.reindex_for_tenant(str(tenant_id), granted)
