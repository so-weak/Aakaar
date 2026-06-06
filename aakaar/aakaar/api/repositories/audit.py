"""Read queries for the audit log. Always tenant-scoped."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aakaar.db.models import AuditLog


def list_for_tenant(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
    action_prefix: str | None = None,
) -> list[AuditLog]:
    stmt = select(AuditLog).where(AuditLog.tenant_id == tenant_id)
    if action_prefix:
        stmt = stmt.where(AuditLog.action.like(f"{action_prefix}%"))
    stmt = stmt.order_by(AuditLog.at.desc()).limit(limit).offset(offset)
    return list(session.scalars(stmt))


def count_for_tenant(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    action_prefix: str | None = None,
) -> int:
    stmt = select(func.count()).select_from(AuditLog).where(
        AuditLog.tenant_id == tenant_id
    )
    if action_prefix:
        stmt = stmt.where(AuditLog.action.like(f"{action_prefix}%"))
    return int(session.scalar(stmt) or 0)
