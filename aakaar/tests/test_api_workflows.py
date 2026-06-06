"""Workflow CRUD + edit-authority tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from aakaar.api.deps import AppDependencies
from aakaar.shared.dag.types import Dag, Edge, Node, NodeKind
from tests._api_helpers import (
    auth_headers,
    login,
    seed_tenant_admin,
    seed_tenant_user,
)


def _simple_dag() -> dict:
    dag = Dag(
        nodes=[
            Node(id="open", kind=NodeKind.ACTION, ref="browser.open_session"),
            Node(
                id="go",
                kind=NodeKind.ACTION,
                ref="browser.navigate",
                inputs={"session": "${open.session}", "url": "https://x"},
            ),
        ],
        edges=[Edge.model_validate({"from": "open", "to": "go"})],
    )
    return dag.model_dump(by_alias=True)


def test_create_get_list_workflow(deps: AppDependencies, client: TestClient) -> None:
    tenant, _ = seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="a@a.test", admin_password="adminpass1"
    )
    seed_tenant_user(
        deps, tenant_id=tenant.id, email="u@a.test", password="userpass1"
    )
    token = login(client, email="u@a.test", password="userpass1")

    r = client.post(
        "/workflows",
        headers=auth_headers(token),
        json={
            "name": "Open and go",
            "description": "demo",
            "dag": _simple_dag(),
            "rationale": "manual",
        },
    )
    assert r.status_code == 201, r.text
    wf = r.json()
    assert wf["latest_version"] == 1

    r = client.get(f"/workflows/{wf['id']}", headers=auth_headers(token))
    assert r.status_code == 200

    r = client.get("/workflows", headers=auth_headers(token))
    assert r.status_code == 200
    assert any(w["id"] == wf["id"] for w in r.json())

    r = client.get(
        f"/workflows/{wf['id']}/versions/latest", headers=auth_headers(token)
    )
    assert r.status_code == 200
    assert r.json()["version"] == 1


def test_only_owner_can_edit(deps: AppDependencies, client: TestClient) -> None:
    tenant, _ = seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="a@a.test", admin_password="adminpass1"
    )
    seed_tenant_user(
        deps, tenant_id=tenant.id, email="owner@a.test", password="ownerpass1"
    )
    seed_tenant_user(
        deps, tenant_id=tenant.id, email="other@a.test", password="otherpass1"
    )

    owner_token = login(client, email="owner@a.test", password="ownerpass1")
    other_token = login(client, email="other@a.test", password="otherpass1")

    wf = client.post(
        "/workflows",
        headers=auth_headers(owner_token),
        json={"name": "wf", "description": "", "dag": _simple_dag()},
    ).json()

    # Other tenant user CAN read it.
    r = client.get(f"/workflows/{wf['id']}", headers=auth_headers(other_token))
    assert r.status_code == 200

    # Other tenant user CANNOT edit.
    r = client.patch(
        f"/workflows/{wf['id']}",
        headers=auth_headers(other_token),
        json={"dag": _simple_dag(), "rationale": "tampering"},
    )
    assert r.status_code == 403

    # Owner can edit; new version saved.
    r = client.patch(
        f"/workflows/{wf['id']}",
        headers=auth_headers(owner_token),
        json={"dag": _simple_dag(), "rationale": "tweak"},
    )
    assert r.status_code == 200
    assert r.json()["version"] == 2


def test_workflow_with_invalid_dag_rejected(
    deps: AppDependencies, client: TestClient
) -> None:
    tenant, _ = seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="a@a.test", admin_password="adminpass1"
    )
    seed_tenant_user(
        deps, tenant_id=tenant.id, email="u@a.test", password="userpass1"
    )
    token = login(client, email="u@a.test", password="userpass1")

    bad_dag = Dag(
        nodes=[
            Node(
                id="go",
                kind=NodeKind.ACTION,
                ref="browser.navigate",
                inputs={"session": "s"},  # missing required url
            )
        ]
    ).model_dump(by_alias=True)

    r = client.post(
        "/workflows",
        headers=auth_headers(token),
        json={"name": "bad", "description": "", "dag": bad_dag},
    )
    assert r.status_code == 422
    assert "missing required input" in r.json()["detail"]


def test_workflow_isolation_across_tenants(
    deps: AppDependencies, client: TestClient
) -> None:
    tenant_a, _ = seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="a@a.test", admin_password="adminpass1"
    )
    seed_tenant_admin(
        deps, slug="globex", name="Globex", admin_email="b@b.test", admin_password="adminpass1"
    )

    seed_tenant_user(
        deps, tenant_id=tenant_a.id, email="u@a.test", password="userpass1"
    )
    a_token = login(client, email="u@a.test", password="userpass1")
    b_token = login(client, email="b@b.test", password="adminpass1")

    wf = client.post(
        "/workflows",
        headers=auth_headers(a_token),
        json={"name": "secret", "description": "", "dag": _simple_dag()},
    ).json()

    # Tenant B cannot see tenant A's workflow.
    r = client.get(f"/workflows/{wf['id']}", headers=auth_headers(b_token))
    assert r.status_code == 404
    r = client.get("/workflows", headers=auth_headers(b_token))
    assert r.status_code == 200
    assert all(w["id"] != wf["id"] for w in r.json())
