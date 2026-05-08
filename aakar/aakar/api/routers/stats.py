"""Role-aware dashboard endpoint.

Tenant users see their own runs; tenant admins see the full tenant.
Cross-tenant (superuser) lives in `routers/superuser.py` to keep the
require_superuser dependency localized to that file.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from aakar.api.deps import get_session, require_tenant_user
from aakar.api.repositories import stats as stats_repo
from aakar.api.schemas import (
    CapabilityUsage,
    DashboardStatsResponse,
    FailureSummary,
    VolumeBucket,
)
from aakar.db.models import User, UserRole


router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/dashboard", response_model=DashboardStatsResponse)
def get_dashboard(
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
) -> DashboardStatsResponse:
    """Auto-scoped: tenant_admin gets full-tenant view; tenant_user gets
    their own runs only."""
    assert user.tenant_id is not None
    if user.role == UserRole.TENANT_ADMIN:
        scope = "tenant"
        tenant_id = user.tenant_id
        user_id = None
    else:
        scope = "user"
        tenant_id = user.tenant_id  # belt-and-suspenders: still tenant-bound
        user_id = user.id

    return _build_dashboard(
        session, scope=scope, tenant_id=tenant_id, user_id=user_id
    )


def _build_dashboard(
    session: Session,
    *,
    scope: str,
    tenant_id,
    user_id,
    include_per_tenant: bool = False,
) -> DashboardStatsResponse:
    """Shared computation. Imported by superuser router for the global
    dashboard, which passes (tenant_id=None, user_id=None) and asks for
    per-tenant breakdown."""
    win24 = stats_repo.window(24)
    win7 = stats_repo.window(24 * 7)
    win30 = stats_repo.window(24 * 30)

    v24 = stats_repo.volume_by_status(
        session, tenant_id=tenant_id, user_id=user_id, since=win24
    )
    v7 = stats_repo.volume_by_status(
        session, tenant_id=tenant_id, user_id=user_id, since=win7
    )
    v30 = stats_repo.volume_by_status(
        session, tenant_id=tenant_id, user_id=user_id, since=win30
    )

    cap_usage = stats_repo.capability_usage(
        session, tenant_id=tenant_id, user_id=user_id, since=win7
    )
    active = stats_repo.active_count(
        session, tenant_id=tenant_id, user_id=user_id
    )
    failures = stats_repo.recent_failures(
        session, tenant_id=tenant_id, user_id=user_id, limit=10
    )

    per_tenant = None
    if include_per_tenant:
        per_tenant = stats_repo.per_tenant_volume(session, since=win24)
        from aakar.api.schemas import TenantVolume

        per_tenant = [TenantVolume(**row) for row in per_tenant]

    return DashboardStatsResponse(
        scope=scope,
        volume_24h=VolumeBucket(**v24),
        volume_7d=VolumeBucket(**v7),
        volume_30d=VolumeBucket(**v30),
        capability_usage=[CapabilityUsage(**c) for c in cap_usage],
        active_count=active,
        recent_failures=[FailureSummary(**f) for f in failures],
        per_tenant=per_tenant,
    )
