"""Tenant repository helpers.

Superuser-only writes; reads scoped to the current tenant for non-superusers
(but in v1 only superusers list tenants, so that's moot here).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from aakar.api.repositories import users as users_repo
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
    """Mark tenant SUSPENDED and cascade-disable every user in it.

    Without the user cascade, existing users could continue to
    authenticate with their previously-issued JWTs (the auth dep checks
    user.status, not tenant.status). `get_current_user` *also* rejects
    users whose tenant is suspended as defence-in-depth, but updating
    the user rows is the source-of-truth fix — it survives any future
    auth path that forgets the tenant check.
    """
    t = session.get(Tenant, tenant_id)
    if t is None:
        return None
    t.status = TenantStatus.SUSPENDED
    users_repo.disable_users_for_tenant(session, tenant_id)
    session.flush()
    return t
