"""Auth + role enforcement tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from aakar.api.deps import AppDependencies
from tests._api_helpers import (
    auth_headers,
    login,
    seed_superuser,
    seed_tenant_admin,
    seed_tenant_user,
)


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_login_success(deps: AppDependencies, client: TestClient) -> None:
    seed_superuser(deps, email="su@aakar.test", password="hunter22-correct")
    r = client.post(
        "/auth/login",
        json={"email": "su@aakar.test", "password": "hunter22-correct"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "Bearer"
    assert isinstance(body["access_token"], str) and body["access_token"]


def test_login_wrong_password(deps: AppDependencies, client: TestClient) -> None:
    seed_superuser(deps, email="su@aakar.test", password="hunter22-correct")
    r = client.post(
        "/auth/login",
        json={"email": "su@aakar.test", "password": "wrong"},
    )
    assert r.status_code == 401


def test_login_unknown_email(client: TestClient) -> None:
    r = client.post(
        "/auth/login",
        json={"email": "nope@aakar.test", "password": "whatever"},
    )
    assert r.status_code == 401


def test_missing_token_rejected(client: TestClient) -> None:
    r = client.get("/superuser/tenants")
    assert r.status_code == 401


def test_role_enforcement(deps: AppDependencies, client: TestClient) -> None:
    tenant, _admin = seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="admin@acme.test", admin_password="adminpass1"
    )
    seed_tenant_user(
        deps, tenant_id=tenant.id, email="user@acme.test", password="userpass1"
    )

    user_token = login(client, email="user@acme.test", password="userpass1")

    # Tenant user must NOT be able to call superuser endpoints.
    r = client.get("/superuser/tenants", headers=auth_headers(user_token))
    assert r.status_code == 403

    # And must NOT be able to create users (admin-only).
    r = client.post(
        "/admin/users",
        headers=auth_headers(user_token),
        json={"email": "x@y.test", "password": "password1", "role": "tenant_user"},
    )
    assert r.status_code == 403


def test_superuser_creates_tenant_then_admin_logs_in(
    deps: AppDependencies, client: TestClient
) -> None:
    seed_superuser(deps, email="su@aakar.test", password="hunter22-correct")
    su_token = login(client, email="su@aakar.test", password="hunter22-correct")

    r = client.post(
        "/superuser/tenants",
        headers=auth_headers(su_token),
        json={
            "slug": "acme",
            "name": "Acme",
            "admin_email": "admin@acme.test",
            "admin_password": "adminpass1",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["slug"] == "acme"

    # Newly created tenant admin can log in.
    admin_token = login(client, email="admin@acme.test", password="adminpass1")
    r = client.get("/admin/users", headers=auth_headers(admin_token))
    assert r.status_code == 200
    emails = [u["email"] for u in r.json()]
    assert "admin@acme.test" in emails
