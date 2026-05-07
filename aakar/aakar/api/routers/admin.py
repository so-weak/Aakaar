"""Tenant-admin endpoints: users + capability grants within the admin's tenant."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from aakar.api.deps import (
    get_capability_index,
    get_session,
    get_vault,
    require_tenant_admin,
)
from aakar.api.repositories import grants as grants_repo
from aakar.api.repositories import users as users_repo
from aakar.api.schemas import (
    GrantCreateRequest,
    GrantResponse,
    UserCreateRequest,
    UserResponse,
)
from aakar.db.models import User
from aakar.planner import CapabilityIndex
from aakar.shared.registry import Registry
from aakar.vault import Vault


router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(
    body: UserCreateRequest,
    admin: Annotated[User, Depends(require_tenant_admin)],
    session: Annotated[Session, Depends(get_session)],
) -> UserResponse:
    try:
        u = users_repo.create_user(
            session,
            tenant_id=admin.tenant_id,
            email=body.email,
            password=body.password,
            role=body.role,
        )
    except users_repo.EmailTaken as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"email taken: {e}") from e
    session.commit()
    return UserResponse(
        id=u.id,
        tenant_id=u.tenant_id,
        email=u.email,
        role=u.role,
        status=u.status,
        created_at=u.created_at,
    )


@router.get("/users", response_model=list[UserResponse])
def list_users(
    admin: Annotated[User, Depends(require_tenant_admin)],
    session: Annotated[Session, Depends(get_session)],
) -> list[UserResponse]:
    assert admin.tenant_id is not None
    return [
        UserResponse(
            id=u.id,
            tenant_id=u.tenant_id,
            email=u.email,
            role=u.role,
            status=u.status,
            created_at=u.created_at,
        )
        for u in users_repo.list_users_for_tenant(session, admin.tenant_id)
    ]


# ---------- grants --------------------------------------------------------


def _registry_dep() -> Registry:
    # Importing get_registry at module-top would create a cycle with deps.py
    # only marginally; this indirection keeps it tidy.
    from aakar.api.deps import get_registry as _get

    return _get  # type: ignore[return-value]


@router.post("/grants", response_model=GrantResponse, status_code=status.HTTP_201_CREATED)
def create_grant(
    body: GrantCreateRequest,
    admin: Annotated[User, Depends(require_tenant_admin)],
    session: Annotated[Session, Depends(get_session)],
    vault: Annotated[Vault, Depends(get_vault)],
    capability_index: Annotated[CapabilityIndex, Depends(get_capability_index)],
) -> GrantResponse:
    assert admin.tenant_id is not None

    # Verify the capability ref is real before storing anything.
    defn = capability_index.registry.get(body.capability_ref)
    if defn is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown capability ref: {body.capability_ref}",
        )

    # Verify the supplied secret names match the capability's declaration.
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

    try:
        grant = grants_repo.create_grant(
            session,
            vault,
            tenant_id=admin.tenant_id,
            created_by=admin.id,
            capability_ref=body.capability_ref,
            account_alias=body.account_alias,
            secrets=body.secrets,
            input_defaults=body.input_defaults,
        )
    except grants_repo.GrantConflict as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e

    session.commit()

    # Refresh the capability index for this tenant so the planner sees the new grant.
    granted = grants_repo.list_granted_refs(session, admin.tenant_id)
    capability_index.reindex_for_tenant(str(admin.tenant_id), granted)

    return GrantResponse(
        id=grant.id,
        capability_ref=grant.capability_ref,
        account_alias=grant.account_alias,
        secret_names=sorted(body.secrets.keys()),
        input_defaults=grant.input_defaults,
        enabled=grant.enabled,
        created_at=grant.created_at,
    )


@router.get("/grants", response_model=list[GrantResponse])
def list_grants(
    admin: Annotated[User, Depends(require_tenant_admin)],
    session: Annotated[Session, Depends(get_session)],
    vault: Annotated[Vault, Depends(get_vault)],
) -> list[GrantResponse]:
    assert admin.tenant_id is not None
    out: list[GrantResponse] = []
    for g in grants_repo.list_grants(session, admin.tenant_id):
        try:
            entry = vault.describe(str(admin.tenant_id), g.vault_ref)
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


@router.delete("/grants/{grant_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_grant(
    grant_id: uuid.UUID,
    admin: Annotated[User, Depends(require_tenant_admin)],
    session: Annotated[Session, Depends(get_session)],
    vault: Annotated[Vault, Depends(get_vault)],
    capability_index: Annotated[CapabilityIndex, Depends(get_capability_index)],
) -> None:
    assert admin.tenant_id is not None
    ok = grants_repo.delete_grant(
        session, vault, tenant_id=admin.tenant_id, grant_id=grant_id
    )
    if not ok:
        raise HTTPException(status_code=404, detail="grant not found")
    session.commit()
    granted = grants_repo.list_granted_refs(session, admin.tenant_id)
    capability_index.reindex_for_tenant(str(admin.tenant_id), granted)
