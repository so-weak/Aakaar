"""Maker-checker governance (segregation of duties).

A workflow can opt in to a governance gate (``requires_approval=True`` or
``sensitivity='elevated'``). When gated, two actions do NOT execute
immediately — publishing a new workflow version (``workflow_publish``) and
starting a run (``run_start``). Instead the action's parameters are snapshotted
into a pending ``ApprovalRequest`` and the API returns 202 with its id. A
*different* user (the checker) then approves or rejects it; the original action
is performed only on approval, under the checker's authorization.
"""

from __future__ import annotations

from aakaar.services.governance.service import (
    GatedAction,
    GovernanceError,
    GovernanceService,
    SelfApprovalError,
    SubjectGoneError,
    workflow_is_gated,
)

__all__ = [
    "GatedAction",
    "GovernanceError",
    "GovernanceService",
    "SelfApprovalError",
    "SubjectGoneError",
    "workflow_is_gated",
]
