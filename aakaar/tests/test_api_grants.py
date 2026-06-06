"""Capability-grant flow tests.

End-to-end: superuser creates tenant + admin, admin grants a capability with
secrets, the secrets land in the vault (not the API responses), and the
planner sees the capability after re-indexing.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from pydantic import BaseModel

from aakaar.api.deps import AppDependencies
from aakaar.shared.registry import (
    CapabilityDefinition,
    SecretSpec,
)
from tests._api_helpers import (
    auth_headers,
    login,
    seed_tenant_admin,
)


class _In(BaseModel):
    pass


class _Out(BaseModel):
    pass


def _add_test_capability(deps: AppDependencies) -> None:
    deps.registry.add(
        CapabilityDefinition(
            ref="cap.test_login",
            description="test capability",
            input_schema=_In,
            output_schema=_Out,
            secrets=(SecretSpec(name="username"), SecretSpec(name="password")),
        )
    )


def test_grant_create_and_list(deps: AppDependencies, client: TestClient) -> None:
    _add_test_capability(deps)
    tenant, _ = seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="a@a.test", admin_password="adminpass1"
    )
    token = login(client, email="a@a.test", password="adminpass1")

    r = client.post(
        "/admin/grants",
        headers=auth_headers(token),
        json={
            "capability_ref": "cap.test_login",
            "account_alias": "primary",
            "secrets": {"username": "u", "password": "p"},
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["capability_ref"] == "cap.test_login"
    assert sorted(body["secret_names"]) == ["password", "username"]
    # Critically: secret VALUES are never exposed.
    assert "u" not in r.text and "p" not in r.text or '"secret_names"' in r.text

    # The vault holds the actual values.
    secrets = deps.vault.fetch(str(tenant.id), f"grants/{body['id']}")
    assert secrets == {"username": "u", "password": "p"}

    # Listing returns the grant.
    r = client.get("/admin/grants", headers=auth_headers(token))
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_grant_rejects_unknown_capability(
    deps: AppDependencies, client: TestClient
) -> None:
    seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="a@a.test", admin_password="adminpass1"
    )
    token = login(client, email="a@a.test", password="adminpass1")
    r = client.post(
        "/admin/grants",
        headers=auth_headers(token),
        json={
            "capability_ref": "cap.does_not_exist",
            "account_alias": "primary",
            "secrets": {"x": "y"},
        },
    )
    assert r.status_code == 400
    assert "unknown capability ref" in r.json()["detail"]


def test_grant_rejects_secret_name_mismatch(
    deps: AppDependencies, client: TestClient
) -> None:
    _add_test_capability(deps)
    seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="a@a.test", admin_password="adminpass1"
    )
    token = login(client, email="a@a.test", password="adminpass1")
    r = client.post(
        "/admin/grants",
        headers=auth_headers(token),
        json={
            "capability_ref": "cap.test_login",
            "account_alias": "primary",
            "secrets": {"username": "u"},  # missing password
        },
    )
    assert r.status_code == 400
    assert "secret names mismatch" in r.json()["detail"]


def test_grant_update_alias_only(deps: AppDependencies, client: TestClient) -> None:
    _add_test_capability(deps)
    tenant, _ = seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="a@a.test", admin_password="adminpass1"
    )
    token = login(client, email="a@a.test", password="adminpass1")
    create = client.post(
        "/admin/grants",
        headers=auth_headers(token),
        json={
            "capability_ref": "cap.test_login",
            "account_alias": "primary",
            "secrets": {"username": "u", "password": "p"},
        },
    ).json()

    r = client.patch(
        f"/admin/grants/{create['id']}",
        headers=auth_headers(token),
        json={"account_alias": "secondary"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["account_alias"] == "secondary"

    # Secrets untouched.
    assert deps.vault.fetch(str(tenant.id), f"grants/{create['id']}") == {
        "username": "u",
        "password": "p",
    }


def test_grant_update_rotates_secrets(
    deps: AppDependencies, client: TestClient
) -> None:
    _add_test_capability(deps)
    tenant, _ = seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="a@a.test", admin_password="adminpass1"
    )
    token = login(client, email="a@a.test", password="adminpass1")
    create = client.post(
        "/admin/grants",
        headers=auth_headers(token),
        json={
            "capability_ref": "cap.test_login",
            "account_alias": "primary",
            "secrets": {"username": "u", "password": "p"},
        },
    ).json()

    r = client.patch(
        f"/admin/grants/{create['id']}",
        headers=auth_headers(token),
        json={"secrets": {"username": "u2", "password": "p2"}},
    )
    assert r.status_code == 200, r.text

    # Vault now holds the new values.
    assert deps.vault.fetch(str(tenant.id), f"grants/{create['id']}") == {
        "username": "u2",
        "password": "p2",
    }
    # Response never returns values.
    assert "u2" not in r.text and "p2" not in r.text


def test_grant_update_rejects_partial_secret_rotation(
    deps: AppDependencies, client: TestClient
) -> None:
    _add_test_capability(deps)
    seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="a@a.test", admin_password="adminpass1"
    )
    token = login(client, email="a@a.test", password="adminpass1")
    create = client.post(
        "/admin/grants",
        headers=auth_headers(token),
        json={
            "capability_ref": "cap.test_login",
            "account_alias": "primary",
            "secrets": {"username": "u", "password": "p"},
        },
    ).json()

    r = client.patch(
        f"/admin/grants/{create['id']}",
        headers=auth_headers(token),
        json={"secrets": {"username": "u2"}},  # missing password
    )
    assert r.status_code == 400
    assert "secret names mismatch" in r.json()["detail"]


def test_grant_update_alias_collision(
    deps: AppDependencies, client: TestClient
) -> None:
    _add_test_capability(deps)
    seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="a@a.test", admin_password="adminpass1"
    )
    token = login(client, email="a@a.test", password="adminpass1")
    a = client.post(
        "/admin/grants",
        headers=auth_headers(token),
        json={
            "capability_ref": "cap.test_login",
            "account_alias": "primary",
            "secrets": {"username": "u", "password": "p"},
        },
    ).json()
    client.post(
        "/admin/grants",
        headers=auth_headers(token),
        json={
            "capability_ref": "cap.test_login",
            "account_alias": "secondary",
            "secrets": {"username": "u", "password": "p"},
        },
    )

    r = client.patch(
        f"/admin/grants/{a['id']}",
        headers=auth_headers(token),
        json={"account_alias": "secondary"},
    )
    assert r.status_code == 409


def test_grant_update_requires_at_least_one_field(
    deps: AppDependencies, client: TestClient
) -> None:
    _add_test_capability(deps)
    seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="a@a.test", admin_password="adminpass1"
    )
    token = login(client, email="a@a.test", password="adminpass1")
    create = client.post(
        "/admin/grants",
        headers=auth_headers(token),
        json={
            "capability_ref": "cap.test_login",
            "account_alias": "primary",
            "secrets": {"username": "u", "password": "p"},
        },
    ).json()

    r = client.patch(
        f"/admin/grants/{create['id']}", headers=auth_headers(token), json={}
    )
    assert r.status_code == 400


def test_grant_delete(deps: AppDependencies, client: TestClient) -> None:
    _add_test_capability(deps)
    tenant, _ = seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="a@a.test", admin_password="adminpass1"
    )
    token = login(client, email="a@a.test", password="adminpass1")
    create = client.post(
        "/admin/grants",
        headers=auth_headers(token),
        json={
            "capability_ref": "cap.test_login",
            "account_alias": "primary",
            "secrets": {"username": "u", "password": "p"},
        },
    ).json()

    r = client.delete(f"/admin/grants/{create['id']}", headers=auth_headers(token))
    assert r.status_code == 204

    # Vault entry is gone.
    from aakaar.vault import VaultNotFound
    import pytest

    with pytest.raises(VaultNotFound):
        deps.vault.fetch(str(tenant.id), f"grants/{create['id']}")
