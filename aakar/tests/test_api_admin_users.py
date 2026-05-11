"""Admin user-management endpoints — edit role/password, suspend, reactivate.

Each test exercises the full HTTP path so the auth, tenant scoping, and
self-block guards are checked together.
"""

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


def _setup(deps: AppDependencies, client: TestClient):
    """Seed a tenant + admin + a regular user. Returns (admin_token, member_user_id)."""
    tenant, _admin = seed_tenant_admin(
        deps,
        slug="acme",
        name="Acme",
        admin_email="admin@acme.test",
        admin_password="adminpass1",
    )
    member = seed_tenant_user(
        deps,
        tenant_id=tenant.id,
        email="member@acme.test",
        password="memberpass1",
    )
    admin_token = login(client, email="admin@acme.test", password="adminpass1")
    return admin_token, str(member.id)


# ---------- edit ----------------------------------------------------------


def test_edit_user_role(deps: AppDependencies, client: TestClient) -> None:
    admin_token, member_id = _setup(deps, client)
    r = client.patch(
        f"/admin/users/{member_id}",
        headers=auth_headers(admin_token),
        json={"role": "tenant_admin"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "tenant_admin"

    # The list reflects the change.
    listed = client.get("/admin/users", headers=auth_headers(admin_token)).json()
    member_row = next(u for u in listed if u["id"] == member_id)
    assert member_row["role"] == "tenant_admin"


def test_edit_user_password_relogins(deps: AppDependencies, client: TestClient) -> None:
    admin_token, member_id = _setup(deps, client)
    r = client.patch(
        f"/admin/users/{member_id}",
        headers=auth_headers(admin_token),
        json={"password": "rotatedpass1"},
    )
    assert r.status_code == 200, r.text

    # Old password rejected, new password accepted.
    r_old = client.post(
        "/auth/login",
        json={"email": "member@acme.test", "password": "memberpass1"},
    )
    assert r_old.status_code == 401
    r_new = client.post(
        "/auth/login",
        json={"email": "member@acme.test", "password": "rotatedpass1"},
    )
    assert r_new.status_code == 200


def test_edit_requires_at_least_one_field(deps: AppDependencies, client: TestClient) -> None:
    admin_token, member_id = _setup(deps, client)
    r = client.patch(
        f"/admin/users/{member_id}",
        headers=auth_headers(admin_token),
        json={},
    )
    assert r.status_code == 400
    assert "at least one" in r.json()["detail"].lower()


def test_edit_rejects_invalid_role(deps: AppDependencies, client: TestClient) -> None:
    admin_token, member_id = _setup(deps, client)
    r = client.patch(
        f"/admin/users/{member_id}",
        headers=auth_headers(admin_token),
        json={"role": "superuser"},  # not assignable from tenant-admin surface
    )
    # Pydantic regex pattern rejects the value — 422 is the correct framework code.
    assert r.status_code == 422


# ---------- suspend / reactivate -----------------------------------------


def test_suspend_user_blocks_login(deps: AppDependencies, client: TestClient) -> None:
    admin_token, member_id = _setup(deps, client)

    # Member can log in initially.
    r = client.post(
        "/auth/login",
        json={"email": "member@acme.test", "password": "memberpass1"},
    )
    assert r.status_code == 200

    # Suspend.
    r = client.post(
        f"/admin/users/{member_id}/suspend",
        headers=auth_headers(admin_token),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "disabled"

    # Now login fails.
    r = client.post(
        "/auth/login",
        json={"email": "member@acme.test", "password": "memberpass1"},
    )
    assert r.status_code == 401


def test_reactivate_user_restores_login(deps: AppDependencies, client: TestClient) -> None:
    admin_token, member_id = _setup(deps, client)
    client.post(
        f"/admin/users/{member_id}/suspend", headers=auth_headers(admin_token)
    )
    r = client.post(
        f"/admin/users/{member_id}/reactivate",
        headers=auth_headers(admin_token),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "active"
    r_login = client.post(
        "/auth/login",
        json={"email": "member@acme.test", "password": "memberpass1"},
    )
    assert r_login.status_code == 200


# ---------- safety guards ------------------------------------------------


def test_admin_cannot_suspend_themselves(
    deps: AppDependencies, client: TestClient
) -> None:
    _, _admin = seed_tenant_admin(
        deps,
        slug="acme",
        name="Acme",
        admin_email="admin@acme.test",
        admin_password="adminpass1",
    )
    admin_token = login(client, email="admin@acme.test", password="adminpass1")
    r = client.post(
        f"/admin/users/{_admin.id}/suspend",
        headers=auth_headers(admin_token),
    )
    assert r.status_code == 400
    assert "themselves" in r.json()["detail"].lower()


def test_admin_cannot_edit_themselves(
    deps: AppDependencies, client: TestClient
) -> None:
    _, _admin = seed_tenant_admin(
        deps,
        slug="acme",
        name="Acme",
        admin_email="admin@acme.test",
        admin_password="adminpass1",
    )
    admin_token = login(client, email="admin@acme.test", password="adminpass1")
    r = client.patch(
        f"/admin/users/{_admin.id}",
        headers=auth_headers(admin_token),
        json={"password": "anotherpass1"},
    )
    assert r.status_code == 400


def test_admin_cannot_touch_other_tenants_user(
    deps: AppDependencies, client: TestClient
) -> None:
    """Admin in tenant A asking for a user in tenant B → 404 (not 403, to
    avoid leaking the existence of cross-tenant users)."""
    _tenant_a, _admin_a = seed_tenant_admin(
        deps,
        slug="acme",
        name="Acme",
        admin_email="admin@acme.test",
        admin_password="adminpass1",
    )
    tenant_b, _admin_b = seed_tenant_admin(
        deps,
        slug="other",
        name="Other Inc",
        admin_email="other@other.test",
        admin_password="otherpass1",
    )
    member_b = seed_tenant_user(
        deps, tenant_id=tenant_b.id, email="bob@other.test", password="bobpass001"
    )
    admin_a_token = login(client, email="admin@acme.test", password="adminpass1")
    for path in (
        f"/admin/users/{member_b.id}/suspend",
        f"/admin/users/{member_b.id}/reactivate",
    ):
        r = client.post(path, headers=auth_headers(admin_a_token))
        assert r.status_code == 404, (path, r.status_code, r.text)
    r = client.patch(
        f"/admin/users/{member_b.id}",
        headers=auth_headers(admin_a_token),
        json={"role": "tenant_user"},
    )
    assert r.status_code == 404


def test_tenant_user_cannot_call_admin_endpoints(
    deps: AppDependencies, client: TestClient
) -> None:
    admin_token, member_id = _setup(deps, client)
    member_token = login(client, email="member@acme.test", password="memberpass1")
    r = client.patch(
        f"/admin/users/{member_id}",
        headers=auth_headers(member_token),
        json={"role": "tenant_admin"},
    )
    assert r.status_code == 403
    r = client.post(
        f"/admin/users/{member_id}/suspend",
        headers=auth_headers(member_token),
    )
    assert r.status_code == 403


# ---------- tenant suspension cascades to users -------------------------


def test_suspend_tenant_disables_all_users_and_blocks_login(
    deps: AppDependencies, client: TestClient
) -> None:
    """Suspending a tenant must lock its users out — both by flipping
    every active user to DISABLED *and* by the auth-layer tenant check."""
    tenant, _admin = seed_tenant_admin(
        deps,
        slug="aarya",
        name="Aarya",
        admin_email="admin@aarya.test",
        admin_password="adminpass1",
    )
    seed_tenant_user(
        deps, tenant_id=tenant.id, email="op@aarya.test", password="oppass1"
    )

    # Both users can log in before suspension.
    r = client.post("/auth/login", json={"email": "admin@aarya.test", "password": "adminpass1"})
    assert r.status_code == 200
    admin_token = r.json()["access_token"]
    r = client.post("/auth/login", json={"email": "op@aarya.test", "password": "oppass1"})
    assert r.status_code == 200

    # Bootstrap a superuser to suspend the tenant.
    seed_superuser(deps, email="root@aakar.test", password="rootpass1")
    su_token = login(client, email="root@aakar.test", password="rootpass1")

    r = client.post(
        f"/superuser/tenants/{tenant.id}/suspend",
        headers=auth_headers(su_token),
    )
    assert r.status_code == 200

    # New logins fail — user rows are now DISABLED.
    r = client.post("/auth/login", json={"email": "admin@aarya.test", "password": "adminpass1"})
    assert r.status_code == 401
    r = client.post("/auth/login", json={"email": "op@aarya.test", "password": "oppass1"})
    assert r.status_code == 401

    # Pre-existing JWTs also stop working — defence-in-depth via the
    # tenant-status check in get_current_user. (A token issued before
    # the suspend would otherwise still be valid until expiry.)
    r = client.get("/admin/users", headers=auth_headers(admin_token))
    assert r.status_code == 401

    # Superuser logins are unaffected — superusers have no tenant_id.
    r = client.get("/superuser/tenants", headers=auth_headers(su_token))
    assert r.status_code == 200
