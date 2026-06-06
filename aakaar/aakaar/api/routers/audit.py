"""Audit log read API. Tenant-admins see their own tenant's audit trail."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from aakaar.api.deps import get_session, require_tenant_admin
from aakaar.api.repositories import audit as audit_repo
from aakaar.api.schemas import AuditEntry, AuditListResponse
from aakaar.db.models import User

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=AuditListResponse)
def list_audit(
    user: Annotated[User, Depends(require_tenant_admin)],
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    action_prefix: Annotated[str | None, Query(max_length=64)] = None,
) -> AuditListResponse:
    assert user.tenant_id is not None
    rows = audit_repo.list_for_tenant(
        session,
        tenant_id=user.tenant_id,
        limit=limit,
        offset=offset,
        action_prefix=action_prefix,
    )
    total = audit_repo.count_for_tenant(
        session, tenant_id=user.tenant_id, action_prefix=action_prefix
    )
    return AuditListResponse(
        entries=[AuditEntry.model_validate(r) for r in rows], total=total
    )
