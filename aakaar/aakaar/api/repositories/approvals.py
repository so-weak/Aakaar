"""Approval-request repository — read/write the `approval_requests` table.

Approval requests are the maker-checker (segregation-of-duties) primitive: a
'maker' raises a pending request to publish a workflow version or start a run;
a 'checker' (a different user) approves or rejects it. The gated action runs
only once a request is approved. Tenant scoping is enforced here so the router
can rely on `None` meaning "not in this tenant" (cross-tenant => 404).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aakaar.api.schemas import ApprovalRequestResponse
from aakaar.db.models import ApprovalRequest, ApprovalStatus


def to_response(req: ApprovalRequest) -> ApprovalRequestResponse:
    """Serialize an approval row to its HTTP shape. Lives here (next to the
    other approval data access) so routers share one mapping and the workflows/
    runs gate hooks don't import the approvals router (which would cycle)."""
    return ApprovalRequestResponse(
        id=req.id,
        tenant_id=req.tenant_id,
        subject_type=req.subject_type,
        subject_ref=req.subject_ref,
        status=req.status,
        requested_by=req.requested_by,
        requested_at=req.requested_at,
        decided_by=req.decided_by,
        decided_at=req.decided_at,
        reason=req.reason,
        context=dict(req.context or {}),
    )


def create_request(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    subject_type: str,
    subject_ref: str,
    requested_by: uuid.UUID,
    context: dict[str, Any] | None = None,
) -> ApprovalRequest:
    req = ApprovalRequest(
        tenant_id=tenant_id,
        subject_type=subject_type,
        subject_ref=subject_ref,
        status=ApprovalStatus.PENDING,
        requested_by=requested_by,
        context=context or {},
    )
    session.add(req)
    session.flush()
    return req


def get_request(
    session: Session, tenant_id: uuid.UUID, request_id: uuid.UUID
) -> ApprovalRequest | None:
    req = session.get(ApprovalRequest, request_id)
    if req is None or req.tenant_id != tenant_id:
        return None
    return req


def list_requests(
    session: Session,
    tenant_id: uuid.UUID,
    *,
    status: str | None = None,
    limit: int = 100,
) -> list[ApprovalRequest]:
    stmt = select(ApprovalRequest).where(ApprovalRequest.tenant_id == tenant_id)
    if status is not None:
        stmt = stmt.where(ApprovalRequest.status == status)
    return list(
        session.scalars(
            stmt.order_by(ApprovalRequest.requested_at.desc()).limit(limit)
        )
    )
