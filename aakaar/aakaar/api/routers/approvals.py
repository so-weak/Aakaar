"""Maker-checker approval endpoints.

A gated publish or run-start opens a pending ``ApprovalRequest`` instead of
acting (see the gate hooks in ``workflows.py`` / ``runs.py``). A *different*
tenant admin — the checker — then decides it here:

  - **approve** records the decision AND performs the originally-gated action
    (publishes the pending workflow version, or starts the pending run) under
    the checker's authorization, attributing it to the original maker.
  - **reject** only records the decision; nothing is performed.

Segregation of duties is enforced by the governance service: the approver may
not be the requester (``SelfApprovalError`` -> 409). Listing/reading is
tenant-scoped, so a cross-tenant request id is a 404.

Performing the approved action can fail *after* the decision is recorded (e.g.
the pinned workflow version was deleted in the meantime). We commit the decision
first, then perform; a performance failure surfaces as 409 with the request
already marked approved, rather than silently swallowing or rolling back an
audited decision.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from aakaar.api.deps import (
    get_audit,
    get_orchestrator,
    get_session,
    require_tenant_admin,
    require_tenant_user,
)
from aakaar.api.repositories import approvals as approvals_repo
from aakaar.api.routers.runs import perform_run_start
from aakaar.api.routers.workflows import perform_publish
from aakaar.api.schemas import (
    ApprovalDecisionRequest,
    ApprovalRequestResponse,
)
from aakaar.db.models import ApprovalSubjectType, User
from aakaar.interpreter import RunOrchestrator
from aakaar.services.audit import AuditRecorder
from aakaar.services.governance import (
    GovernanceError,
    GovernanceService,
    SelfApprovalError,
    SubjectGoneError,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/approvals", tags=["approvals"])
_governance = GovernanceService()


def _get_request_or_404(
    session: Session, tenant_id: uuid.UUID, request_id: uuid.UUID
) -> ApprovalRequestResponse:
    req = approvals_repo.get_request(session, tenant_id, request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="approval request not found")
    return approvals_repo.to_response(req)


@router.get("", response_model=list[ApprovalRequestResponse])
def list_approvals(
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
    status_filter: Annotated[
        str | None,
        Query(alias="status", pattern=r"^(pending|approved|rejected|cancelled)$"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[ApprovalRequestResponse]:
    """List the tenant's approval requests, newest first. Any tenant user can
    see them (so a maker can watch their own request); deciding is admin-only."""
    assert user.tenant_id is not None
    rows = approvals_repo.list_requests(
        session, user.tenant_id, status=status_filter, limit=limit
    )
    return [approvals_repo.to_response(r) for r in rows]


@router.get("/{request_id}", response_model=ApprovalRequestResponse)
def get_approval(
    request_id: uuid.UUID,
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
) -> ApprovalRequestResponse:
    assert user.tenant_id is not None
    return _get_request_or_404(session, user.tenant_id, request_id)


def _decide_and_audit(
    *,
    session: Session,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    approver_id: uuid.UUID,
    approve: bool,
    reason: str,
    audit: AuditRecorder,
) -> tuple[str, str, uuid.UUID]:
    """Record the maker-checker decision, mapping governance errors to HTTP and
    committing it. Returns (subject_type, subject_ref, requested_by) captured
    before the session is reused, so the caller can perform the action without
    re-reading the row. The decision is durable before any action runs."""
    try:
        req = _governance.decide(
            session,
            tenant_id=tenant_id,
            request_id=request_id,
            approver_id=approver_id,
            approve=approve,
            reason=reason,
        )
    except SelfApprovalError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except SubjectGoneError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except GovernanceError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    subject_type = req.subject_type
    subject_ref = req.subject_ref
    requested_by = req.requested_by
    session.commit()
    audit.record(
        action="approval.approve" if approve else "approval.reject",
        tenant_id=tenant_id,
        actor_id=approver_id,
        target_kind="approval_request",
        target_id=str(request_id),
        payload={
            "subject_type": subject_type,
            "subject_ref": subject_ref,
            "requested_by": str(requested_by),
            "reason": reason,
        },
    )
    return subject_type, subject_ref, requested_by


@router.post("/{request_id}/approve", response_model=ApprovalRequestResponse)
async def approve_request(
    request_id: uuid.UUID,
    body: ApprovalDecisionRequest,
    user: Annotated[User, Depends(require_tenant_admin)],
    session: Annotated[Session, Depends(get_session)],
    orchestrator: Annotated[RunOrchestrator, Depends(get_orchestrator)],
    audit: Annotated[AuditRecorder, Depends(get_audit)],
) -> ApprovalRequestResponse:
    """Approve a pending request and PERFORM the gated action.

    The checker (a tenant admin who is not the maker) authorizes the action;
    it executes attributed to the original maker (``requested_by``). The
    decision is committed before the action runs, so an action failure leaves an
    audited 'approved' request and a 409 — not a silently-lost approval."""
    assert user.tenant_id is not None
    subject_type, _subject_ref, requested_by = _decide_and_audit(
        session=session,
        tenant_id=user.tenant_id,
        request_id=request_id,
        approver_id=user.id,
        approve=True,
        reason=body.reason,
        audit=audit,
    )

    # Re-read the (now-approved) request to get its frozen context snapshot.
    approved = approvals_repo.get_request(session, user.tenant_id, request_id)
    assert approved is not None  # just decided it in this tenant
    context = dict(approved.context or {})

    if subject_type == ApprovalSubjectType.WORKFLOW_PUBLISH:
        try:
            version = perform_publish(
                session,
                tenant_id=user.tenant_id,
                created_by=requested_by,
                context=context,
            )
        except (KeyError, LookupError) as e:
            raise HTTPException(
                status_code=409,
                detail=f"approved publish could not be performed: {e}",
            ) from e
        session.commit()
        logger.info(
            "approval %s performed publish workflow version=%s by checker=%s",
            request_id,
            version.version,
            user.id,
        )
    elif subject_type == ApprovalSubjectType.RUN_START:
        try:
            run = perform_run_start(
                session,
                orchestrator=orchestrator,
                tenant_id=user.tenant_id,
                started_by=requested_by,
                context=context,
            )
        except (KeyError, LookupError) as e:
            raise HTTPException(
                status_code=409,
                detail=f"approved run could not be started: {e}",
            ) from e
        logger.info(
            "approval %s started run=%s by checker=%s", request_id, run.id, user.id
        )
    else:
        # Unknown subject types are recorded as approved but have no action to
        # perform — log loudly rather than fail the decision.
        logger.warning(
            "approval %s approved with unhandled subject_type=%s; nothing performed",
            request_id,
            subject_type,
        )

    return _get_request_or_404(session, user.tenant_id, request_id)


@router.post("/{request_id}/reject", response_model=ApprovalRequestResponse)
def reject_request(
    request_id: uuid.UUID,
    body: ApprovalDecisionRequest,
    user: Annotated[User, Depends(require_tenant_admin)],
    session: Annotated[Session, Depends(get_session)],
    audit: Annotated[AuditRecorder, Depends(get_audit)],
) -> ApprovalRequestResponse:
    """Reject a pending request. Records the decision; nothing is performed."""
    assert user.tenant_id is not None
    _decide_and_audit(
        session=session,
        tenant_id=user.tenant_id,
        request_id=request_id,
        approver_id=user.id,
        approve=False,
        reason=body.reason,
        audit=audit,
    )
    logger.info("approval %s rejected by checker=%s", request_id, user.id)
    return _get_request_or_404(session, user.tenant_id, request_id)
