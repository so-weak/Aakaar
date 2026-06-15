"""Maker-checker governance service — segregation-of-duties enforcement.

This is the decision core, deliberately decoupled from *performing* the gated
action. When a gated action is attempted, the API opens a pending
``ApprovalRequest`` instead of executing; a different user later decides it.
On approval the API (not this service) reads the approved request and performs
the original publish/run-start under the checker's authorization — that keeps
this layer free of router/orchestrator imports and trivially testable.

The one rule this service exists to guarantee is segregation of duties: the
approver must not be the requester (``SelfApprovalError``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from aakaar.api.repositories import approvals as approvals_repo
from aakaar.db.models import ApprovalRequest, ApprovalStatus


class GovernanceError(Exception):
    """Base class for governance gate violations."""


class SelfApprovalError(GovernanceError):
    """The approver is the same user that requested the action (SoD breach)."""


class SubjectGoneError(GovernanceError):
    """The approval request does not exist (or not in this tenant)."""


def workflow_is_gated(*, requires_approval: bool, sensitivity: str) -> bool:
    """Whether a workflow's actions must go through maker-checker.

    Gated when the workflow opts in explicitly or is marked elevated, so the
    default (``requires_approval=False``, ``sensitivity='normal'``) preserves
    today's behaviour and existing flows are unaffected.
    """
    return bool(requires_approval) or sensitivity == "elevated"


@dataclass(frozen=True, slots=True)
class GatedAction:
    """A snapshot of the action a maker wants to perform, pending approval.

    ``subject_ref`` identifies the gated resource (workflow id, version id, or a
    pending-run correlation id). ``context`` is everything a checker needs to
    decide without chasing other tables (workflow name, sensitivity, inputs
    summary). The API reconstructs and performs the action from these on
    approval.
    """

    subject_type: str
    subject_ref: str
    context: dict[str, Any]


class GovernanceService:
    """Opens approval gates and records maker-checker decisions."""

    def open_gate(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        action: GatedAction,
        requested_by: uuid.UUID,
    ) -> ApprovalRequest:
        """Create a pending approval for ``action`` and return it.

        The caller raised the action while gated; instead of executing it, a
        pending ``ApprovalRequest`` is recorded for a checker to decide.
        """
        return approvals_repo.create_request(
            session,
            tenant_id=tenant_id,
            subject_type=action.subject_type,
            subject_ref=action.subject_ref,
            requested_by=requested_by,
            context=action.context,
        )

    def decide(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        request_id: uuid.UUID,
        approver_id: uuid.UUID,
        approve: bool,
        reason: str = "",
    ) -> ApprovalRequest:
        """Approve or reject a pending request, enforcing segregation of duties.

        Raises ``SubjectGoneError`` if the request is absent/cross-tenant,
        ``GovernanceError`` if it is not pending, and ``SelfApprovalError`` if
        the approver is the original requester. The approved action itself is
        performed by the caller, not here.
        """
        req = approvals_repo.get_request(session, tenant_id, request_id)
        if req is None:
            raise SubjectGoneError(f"approval request {request_id} not found")
        if req.status != ApprovalStatus.PENDING:
            raise GovernanceError(
                f"approval request {request_id} is already {req.status}"
            )
        if req.requested_by == approver_id:
            raise SelfApprovalError(
                "the maker cannot be the checker — approval requires a different user"
            )
        req.status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
        req.decided_by = approver_id
        req.decided_at = datetime.now(UTC)
        req.reason = reason
        session.flush()
        return req
