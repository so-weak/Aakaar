"""Helpers for API tests — login + simple request shortcuts.

Kept separate from conftest so individual tests can opt in. Tests that
exercise unauthenticated edge cases shouldn't pre-bootstrap users.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from aakaar.api.auth import hash_password
from aakaar.api.deps import AppDependencies
from aakaar.db.models import Tenant, TenantStatus, User, UserRole, UserStatus


def seed_superuser(deps: AppDependencies, *, email: str, password: str) -> User:
    with deps.session_factory.session() as s:
        user = User(
            id=uuid.uuid4(),
            tenant_id=None,
            email=email,
            password_hash=hash_password(password),
            role=UserRole.SUPERUSER,
            status=UserStatus.ACTIVE,
        )
        s.add(user)
        s.commit()
        s.refresh(user)
        return user


def seed_tenant_admin(
    deps: AppDependencies,
    *,
    slug: str,
    name: str,
    admin_email: str,
    admin_password: str,
) -> tuple[Tenant, User]:
    with deps.session_factory.session() as s:
        tenant = Tenant(id=uuid.uuid4(), slug=slug, name=name, status=TenantStatus.ACTIVE)
        s.add(tenant)
        s.flush()
        admin = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            email=admin_email,
            password_hash=hash_password(admin_password),
            role=UserRole.TENANT_ADMIN,
            status=UserStatus.ACTIVE,
        )
        s.add(admin)
        s.commit()
        s.refresh(tenant)
        s.refresh(admin)
        return tenant, admin


def seed_tenant_user(
    deps: AppDependencies,
    *,
    tenant_id: uuid.UUID,
    email: str,
    password: str,
) -> User:
    with deps.session_factory.session() as s:
        user = User(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            email=email,
            password_hash=hash_password(password),
            role=UserRole.TENANT_USER,
            status=UserStatus.ACTIVE,
        )
        s.add(user)
        s.commit()
        s.refresh(user)
        return user


def login(client: TestClient, *, email: str, password: str) -> str:
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
