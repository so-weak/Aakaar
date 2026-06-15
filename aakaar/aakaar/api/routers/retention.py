"""Retention / legal-hold / right-to-erasure API (tenant-admin).

All routes are tenant-scoped: an admin manages retention only within their own
tenant. A legal-hold or erasure request that names a resource belonging to
another tenant (or no tenant) returns 404 — the same opaque "not found" the
rest of the API uses so cross-tenant existence can't be probed.

Routes:
  - ``GET  /retention/policies``                 list this tenant's policies
  - ``GET  /retention/policies/{resource_type}`` one policy (404 if unset)
  - ``PUT  /retention/policies/{resource_type}`` create/update a policy
  - ``POST /retention/legal-hold``               set/clear a hold on a resource
  - ``POST /retention/erase``                    erase one resource on demand

The service owns the durability + audit; this router is a thin tenant-scoped
authz + validation layer that maps service errors to HTTP status codes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from aakaar.api.deps import AppDependencies, get_deps, require_tenant_admin
from aakaar.db.models import User
from aakaar.services.retention import (
    ERASABLE_RESOURCE_TYPES,
    LegalHoldError,
    RetentionError,
    RetentionService,
    UnknownResourceError,
)

router = APIRouter(prefix="/retention", tags=["retention"])


# ---- dependency -----------------------------------------------------------


def get_retention(
    deps: Annotated[AppDependencies, Depends(get_deps)],
) -> RetentionService:
    """Build a RetentionService from the long-lived app components.

    Construction is cheap (it only captures references), so we build per
    request rather than threading another field through AppDependencies —
    keeping the wiring confined to the retention surface.
    """
    return RetentionService(
        session_factory=deps.session_factory,
        object_store=deps.object_store,
        audit=deps.audit,
    )


# ---- schemas --------------------------------------------------------------


class RetentionPolicyResponse(BaseModel):
    resource_type: str
    ttl_days: int | None
    updated_at: datetime
    updated_by: uuid.UUID | None


class RetentionPolicyUpdate(BaseModel):
    ttl_days: int | None = Field(
        default=None,
        ge=1,
        description="Days to retain; null = retain indefinitely. Must be >= 1.",
    )


class LegalHoldRequest(BaseModel):
    resource_type: str = Field(description="One of: run, stored_object.")
    resource_id: uuid.UUID
    hold: bool = Field(description="True to set the hold, false to clear it.")


class EraseRequest(BaseModel):
    resource_type: str = Field(description="One of: run, stored_object.")
    resource_id: uuid.UUID
    reason: str = Field(default="", max_length=512)


class EraseResponse(BaseModel):
    resource_type: str
    resource_id: uuid.UUID
    erased_at: datetime
    already_erased: bool


# ---- helpers --------------------------------------------------------------


def _validate_resource_type(resource_type: str) -> None:
    if resource_type not in ERASABLE_RESOURCE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"unsupported resource_type {resource_type!r}; "
                f"expected one of {sorted(ERASABLE_RESOURCE_TYPES)}"
            ),
        )


# ---- policies -------------------------------------------------------------


@router.get("/policies", response_model=list[RetentionPolicyResponse])
def list_policies(
    admin: Annotated[User, Depends(require_tenant_admin)],
    retention: Annotated[RetentionService, Depends(get_retention)],
) -> list[RetentionPolicyResponse]:
    assert admin.tenant_id is not None
    return [
        RetentionPolicyResponse(
            resource_type=p.resource_type,
            ttl_days=p.ttl_days,
            updated_at=p.updated_at,
            updated_by=p.updated_by,
        )
        for p in retention.list_policies(admin.tenant_id)
    ]


@router.get("/policies/{resource_type}", response_model=RetentionPolicyResponse)
def get_policy(
    resource_type: str,
    admin: Annotated[User, Depends(require_tenant_admin)],
    retention: Annotated[RetentionService, Depends(get_retention)],
) -> RetentionPolicyResponse:
    assert admin.tenant_id is not None
    _validate_resource_type(resource_type)
    policy = retention.get_policy(admin.tenant_id, resource_type)
    if policy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no retention policy for {resource_type}",
        )
    return RetentionPolicyResponse(
        resource_type=policy.resource_type,
        ttl_days=policy.ttl_days,
        updated_at=policy.updated_at,
        updated_by=policy.updated_by,
    )


@router.put("/policies/{resource_type}", response_model=RetentionPolicyResponse)
def put_policy(
    resource_type: str,
    body: RetentionPolicyUpdate,
    admin: Annotated[User, Depends(require_tenant_admin)],
    retention: Annotated[RetentionService, Depends(get_retention)],
) -> RetentionPolicyResponse:
    """Create or update the policy for a resource type in the admin's tenant."""
    assert admin.tenant_id is not None
    _validate_resource_type(resource_type)
    try:
        policy = retention.upsert_policy(
            tenant_id=admin.tenant_id,
            resource_type=resource_type,
            ttl_days=body.ttl_days,
            updated_by=admin.id,
        )
    except RetentionError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return RetentionPolicyResponse(
        resource_type=policy.resource_type,
        ttl_days=policy.ttl_days,
        updated_at=policy.updated_at,
        updated_by=policy.updated_by,
    )


# ---- legal hold -----------------------------------------------------------


@router.post("/legal-hold", status_code=status.HTTP_204_NO_CONTENT)
def set_legal_hold(
    body: LegalHoldRequest,
    admin: Annotated[User, Depends(require_tenant_admin)],
    retention: Annotated[RetentionService, Depends(get_retention)],
) -> None:
    """Set or clear a legal hold on a run or stored object in the admin's tenant."""
    assert admin.tenant_id is not None
    _validate_resource_type(body.resource_type)
    try:
        retention.set_legal_hold(
            tenant_id=admin.tenant_id,
            resource_type=body.resource_type,
            resource_id=body.resource_id,
            hold=body.hold,
            actor_id=admin.id,
        )
    except UnknownResourceError as e:
        # Cross-tenant or absent -> opaque 404.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="resource not found"
        ) from e


# ---- erasure --------------------------------------------------------------


@router.post("/erase", response_model=EraseResponse)
def erase_resource(
    body: EraseRequest,
    admin: Annotated[User, Depends(require_tenant_admin)],
    retention: Annotated[RetentionService, Depends(get_retention)],
) -> EraseResponse:
    """Right-to-erasure for one resource in the admin's tenant.

    409 if the resource is under legal hold; 404 if absent/cross-tenant.
    """
    assert admin.tenant_id is not None
    _validate_resource_type(body.resource_type)
    try:
        result = retention.erase_resource(
            tenant_id=admin.tenant_id,
            resource_type=body.resource_type,
            resource_id=body.resource_id,
            actor_id=admin.id,
            reason=body.reason,
        )
    except LegalHoldError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="resource is under legal hold; clear it before erasing",
        ) from e
    except UnknownResourceError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="resource not found"
        ) from e
    return EraseResponse(
        resource_type=result.resource_type,
        resource_id=result.resource_id,
        erased_at=result.erased_at,
        already_erased=result.already_erased,
    )
