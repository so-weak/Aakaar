"""Aggregate queries for the dashboard.

Three scopes:
  - "user":   runs started_by a specific user (personal dashboard)
  - "tenant": every run within a tenant (tenant_admin dashboard)
  - "global": cross-tenant (superuser dashboard)

Each helper returns Python primitives the router converts to schemas.
We keep the query logic here so the router stays declarative and the
response types live in schemas.py.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aakaar.db.models import (
    Run,
    RunEvent,
    RunEventKind,
    RunStatus,
    Tenant,
    Workflow,
)

# ---------- types -------------------------------------------------------


_ACTIVE_STATUSES: tuple[str, ...] = (
    RunStatus.QUEUED,
    RunStatus.RUNNING,
    RunStatus.PAUSED,
)


# ---------- helpers ----------------------------------------------------


def _scope_filter(stmt, *, tenant_id: uuid.UUID | None, user_id: uuid.UUID | None):
    """Apply (tenant_id, user_id) constraints to a Run-based query."""
    if tenant_id is not None:
        stmt = stmt.where(Run.tenant_id == tenant_id)
    if user_id is not None:
        stmt = stmt.where(Run.started_by == user_id)
    return stmt


def _scope_filter_event(
    stmt, *, tenant_id: uuid.UUID | None, user_id: uuid.UUID | None
):
    """Apply scope to a RunEvent query. user_id requires a join through Run."""
    if tenant_id is not None:
        stmt = stmt.where(RunEvent.tenant_id == tenant_id)
    if user_id is not None:
        stmt = stmt.join(Run, Run.id == RunEvent.run_id).where(
            Run.started_by == user_id
        )
    return stmt


# ---------- volume + status breakdown ----------------------------------


def volume_by_status(
    session: Session,
    *,
    tenant_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    since: datetime,
) -> dict[str, int]:
    """Returns {status: count} for runs started since `since`.

    Statuses with zero runs are still represented (key present, value 0)
    so the UI doesn't have to deal with missing keys.
    """
    stmt = (
        select(Run.status, func.count(Run.id))
        .where(Run.started_at >= since)
        .group_by(Run.status)
    )
    stmt = _scope_filter(stmt, tenant_id=tenant_id, user_id=user_id)
    rows = session.execute(stmt).all()
    out: dict[str, int] = {
        RunStatus.QUEUED: 0,
        RunStatus.RUNNING: 0,
        RunStatus.PAUSED: 0,
        RunStatus.SUCCEEDED: 0,
        RunStatus.FAILED: 0,
        RunStatus.CANCELLED: 0,
    }
    for status, count in rows:
        out[status] = int(count)
    return out


def active_count(
    session: Session,
    *,
    tenant_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
) -> int:
    stmt = select(func.count(Run.id)).where(Run.status.in_(_ACTIVE_STATUSES))
    stmt = _scope_filter(stmt, tenant_id=tenant_id, user_id=user_id)
    return int(session.scalar(stmt) or 0)


# ---------- capability usage -------------------------------------------


def capability_usage(
    session: Session,
    *,
    tenant_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    since: datetime,
    limit: int = 20,
) -> list[dict[str, int | str]]:
    """Capability usage breakdown derived from run_events.

    NODE_COMPLETED → success count, NODE_FAILED → failure count, both
    grouped by `payload->ref` (the capability ref). Returns the top
    `limit` refs by total count.
    """
    completed: dict[str, int] = defaultdict(int)
    failed: dict[str, int] = defaultdict(int)

    for kind, target in (
        (RunEventKind.NODE_COMPLETED, completed),
        (RunEventKind.NODE_FAILED, failed),
    ):
        stmt = select(RunEvent.payload).where(
            RunEvent.kind == kind, RunEvent.at >= since
        )
        stmt = _scope_filter_event(stmt, tenant_id=tenant_id, user_id=user_id)
        for (payload,) in session.execute(stmt).all():
            ref = (payload or {}).get("ref")
            if isinstance(ref, str) and ref:
                target[ref] += 1

    refs = set(completed.keys()) | set(failed.keys())
    rows = [
        {
            "capability_ref": r,
            "count": completed.get(r, 0) + failed.get(r, 0),
            "failure_count": failed.get(r, 0),
        }
        for r in refs
    ]
    rows.sort(key=lambda x: x["count"], reverse=True)  # type: ignore[arg-type]
    return rows[:limit]


# ---------- recent failures --------------------------------------------


def recent_failures(
    session: Session,
    *,
    tenant_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    limit: int = 10,
) -> list[dict]:
    """Most recent failed runs, with the workflow name + error joined in.

    Returned dicts include `tenant_slug` only when the scope is global —
    that's how the UI distinguishes a tenant_admin dashboard from a
    superuser one without an extra query.
    """
    stmt = (
        select(Run, Workflow.name, Tenant.slug)
        .join(Workflow, Workflow.id == Run.workflow_id)
        .join(Tenant, Tenant.id == Run.tenant_id)
        .where(Run.status == RunStatus.FAILED)
    )
    stmt = _scope_filter(stmt, tenant_id=tenant_id, user_id=user_id)
    stmt = stmt.order_by(Run.started_at.desc()).limit(limit)

    out: list[dict] = []
    cross_tenant = tenant_id is None and user_id is None
    for run, wf_name, t_slug in session.execute(stmt).all():
        err = run.error or {}
        out.append(
            {
                "run_id": run.id,
                "workflow_id": run.workflow_id,
                "workflow_name": wf_name,
                "started_at": run.started_at,
                "ended_at": run.ended_at,
                "error_type": err.get("type", "Unknown"),
                "error_message": err.get("message", ""),
                "tenant_slug": t_slug if cross_tenant else None,
            }
        )
    return out


# ---------- per-tenant breakdown (global scope only) -------------------


def per_tenant_volume(session: Session, *, since: datetime) -> list[dict]:
    """For the superuser dashboard: per-tenant run volume + success rate
    over the window. Active tenants only (tenants with zero runs are
    omitted; the tenant list page is the right surface for those)."""
    stmt = (
        select(
            Run.tenant_id,
            Tenant.slug,
            Tenant.name,
            Run.status,
            func.count(Run.id),
        )
        .join(Tenant, Tenant.id == Run.tenant_id)
        .where(Run.started_at >= since)
        .group_by(Run.tenant_id, Tenant.slug, Tenant.name, Run.status)
    )
    by_tenant: dict[uuid.UUID, dict] = {}
    for tid, slug, name, status, count in session.execute(stmt).all():
        bucket = by_tenant.setdefault(
            tid,
            {
                "tenant_id": tid,
                "tenant_slug": slug,
                "tenant_name": name,
                "total": 0,
                "succeeded": 0,
                "failed": 0,
            },
        )
        bucket["total"] += int(count)
        if status == RunStatus.SUCCEEDED:
            bucket["succeeded"] += int(count)
        elif status == RunStatus.FAILED:
            bucket["failed"] += int(count)

    rows = list(by_tenant.values())
    for r in rows:
        terminal = r["succeeded"] + r["failed"]
        r["success_rate"] = (
            r["succeeded"] / terminal if terminal > 0 else None
        )
    rows.sort(key=lambda r: r["total"], reverse=True)
    return rows


# ---------- daily volume time series -----------------------------------


_IST = timezone(timedelta(hours=5, minutes=30))


def daily_volume(
    session: Session,
    *,
    tenant_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    days: int = 30,
) -> list[dict]:
    """Per-IST-day status counts for the last `days` days.

    Returns a contiguous series (empty days included as zeros) so the
    chart x-axis is gap-free. Bucketing happens in Python because the
    test harness uses SQLite and `AT TIME ZONE` isn't portable; the
    row volume here is bounded by `days * runs_per_day` which is fine
    for v1 dashboard scale.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    stmt = select(Run.started_at, Run.status).where(Run.started_at >= since)
    stmt = _scope_filter(stmt, tenant_id=tenant_id, user_id=user_id)

    buckets: dict[str, dict[str, int]] = {}
    for started_at, status in session.execute(stmt).all():
        # Normalize naive timestamps (older SQLite rows) into UTC before
        # converting; `astimezone()` raises on naive datetimes.
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        local = started_at.astimezone(_IST)
        key = local.strftime("%Y-%m-%d")
        bucket = buckets.setdefault(key, {})
        bucket[status] = bucket.get(status, 0) + 1

    today_ist = datetime.now(_IST).date()
    out: list[dict] = []
    for i in range(days - 1, -1, -1):
        d = today_ist - timedelta(days=i)
        key = d.isoformat()
        b = buckets.get(key, {})
        out.append(
            {
                "date": key,
                "succeeded": b.get(RunStatus.SUCCEEDED, 0),
                "failed": b.get(RunStatus.FAILED, 0),
                "paused": b.get(RunStatus.PAUSED, 0),
                "running": b.get(RunStatus.RUNNING, 0),
                "queued": b.get(RunStatus.QUEUED, 0),
                "cancelled": b.get(RunStatus.CANCELLED, 0),
            }
        )
    return out


# ---------- window helper ----------------------------------------------


def window(hours: int) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours)
