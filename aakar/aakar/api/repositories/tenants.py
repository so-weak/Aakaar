"""Tenant repository helpers.

Superuser-only writes; reads scoped to the current tenant for non-superusers
(but in v1 only superusers list tenants, so that's moot here).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from aakar.db.models import Tenant, TenantStatus


class TenantSlugTaken(ValueError):
    pass


def create_tenant(session: Session, *, slug: str, name: str) -> Tenant:
    existing = session.scalars(select(Tenant).where(Tenant.slug == slug)).first()
    if existing is not None:
        raise TenantSlugTaken(slug)
    t = Tenant(slug=slug, name=name, status=TenantStatus.ACTIVE)
    session.add(t)
    session.flush()
    return t


def list_tenants(session: Session) -> list[Tenant]:
    return list(session.scalars(select(Tenant).order_by(Tenant.created_at)))


def get_tenant(session: Session, tenant_id: uuid.UUID) -> Tenant | None:
    return session.get(Tenant, tenant_id)


def suspend_tenant(session: Session, tenant_id: uuid.UUID) -> Tenant | None:
    t = session.get(Tenant, tenant_id)
    if t is None:
        return None
    t.status = TenantStatus.SUSPENDED
    session.flush()
    return t
