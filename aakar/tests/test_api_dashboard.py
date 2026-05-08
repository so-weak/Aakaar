"""Dashboard endpoint tests.

End-to-end: run a successful + a failed workflow, then verify the
three dashboard scopes (user, tenant, global) return the right shape
and the failure shows up in `recent_failures`.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from aakar.api.deps import AppDependencies
from aakar.shared.dag.types import Dag, Node, NodeKind
from tests._api_helpers import (
    auth_headers,
    login,
    seed_superuser,
    seed_tenant_admin,
    seed_tenant_user,
)


def _save_workflow(client: TestClient, token: str, dag: Dag, *, name: str = "demo") -> str:
    r = client.post(
        "/workflows",
        headers=auth_headers(token),
        json={"name": name, "description": "", "dag": dag.model_dump(by_alias=True)},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _wait_for_terminal(
    client: TestClient, token: str, run_id: str, timeout: float = 5.0
) -> dict:
    start = time.time()
    while time.time() - start < timeout:
        r = client.get(f"/runs/{run_id}", headers=auth_headers(token)).json()
        if r["run"]["status"] in ("succeeded", "failed", "cancelled"):
            return r
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish in {timeout}s")


def _seed_runs(deps: AppDependencies, client: TestClient) -> tuple[str, str]:
    """Seed tenant + admin + user, kick off one passing and one failing run.

    Returns (admin_token, user_token) once both runs have terminated.
    """
    tenant, _ = seed_tenant_admin(
        deps,
        slug="acme",
        name="Acme",
        admin_email="a@a.test",
        admin_password="adminpass1",
    )
    seed_tenant_user(deps, tenant_id=tenant.id, email="u@a.test", password="userpass1")
    user_token = login(client, email="u@a.test", password="userpass1")
    admin_token = login(client, email="a@a.test", password="adminpass1")

    pass_dag = Dag(
        nodes=[
            Node(
                id="w",
                kind=NodeKind.CONTROL,
                ref="control.wait",
                inputs={"seconds": 0.01},
            )
        ]
    )
    wf_pass = _save_workflow(client, user_token, pass_dag, name="passes")
    r = client.post(
        f"/workflows/{wf_pass}/runs", headers=auth_headers(user_token), json={}
    )
    assert r.status_code == 201
    _wait_for_terminal(client, user_token, r.json()["id"])

    fail_dag = Dag(
        nodes=[
            Node(
                id="bad",
                kind=NodeKind.ACTION,
                ref="http.request",
                inputs={
                    "method": "GET",
                    "url": "http://127.0.0.1:1/never",
                    "timeout_ms": 200,
                },
            )
        ]
    )
    wf_fail = _save_workflow(client, user_token, fail_dag, name="fails")
    r = client.post(
        f"/workflows/{wf_fail}/runs", headers=auth_headers(user_token), json={}
    )
    assert r.status_code == 201
    _wait_for_terminal(client, user_token, r.json()["id"])

    return admin_token, user_token


def test_dashboard_user_scope(deps: AppDependencies, client: TestClient) -> None:
    _, user_token = _seed_runs(deps, client)

    r = client.get("/stats/dashboard", headers=auth_headers(user_token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "user"
    # Both runs were started by this user.
    assert body["volume_24h"]["succeeded"] == 1
    assert body["volume_24h"]["failed"] == 1
    # Failure surfaces in recent_failures.
    assert len(body["recent_failures"]) == 1
    assert body["recent_failures"][0]["workflow_name"] == "fails"
    # User-scope dashboards never include per_tenant.
    assert body["per_tenant"] is None


def test_dashboard_tenant_scope(deps: AppDependencies, client: TestClient) -> None:
    admin_token, _ = _seed_runs(deps, client)

    r = client.get("/stats/dashboard", headers=auth_headers(admin_token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "tenant"
    assert body["volume_24h"]["succeeded"] == 1
    assert body["volume_24h"]["failed"] == 1
    # Capability usage tracks node_completed events. http.request failed,
    # so only control.wait should have a successful completion.
    refs = {c["capability_ref"] for c in body["capability_usage"]}
    assert "control.wait" in refs


def test_superuser_dashboard_global(deps: AppDependencies, client: TestClient) -> None:
    _seed_runs(deps, client)
    seed_superuser(deps, email="root@aakar.test", password="rootpass1")
    su_token = login(client, email="root@aakar.test", password="rootpass1")

    r = client.get("/superuser/stats/dashboard", headers=auth_headers(su_token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "global"
    # Per-tenant breakdown is the superuser's distinguishing feature.
    assert body["per_tenant"] is not None
    assert len(body["per_tenant"]) == 1
    assert body["per_tenant"][0]["tenant_slug"] == "acme"
    # Cross-tenant failures get the tenant_slug stamped onto each row.
    assert all(f["tenant_slug"] == "acme" for f in body["recent_failures"])


def test_dashboard_requires_auth(deps: AppDependencies, client: TestClient) -> None:
    r = client.get("/stats/dashboard")
    assert r.status_code == 401


def test_superuser_dashboard_blocks_tenant_admin(
    deps: AppDependencies, client: TestClient
) -> None:
    admin_token, _ = _seed_runs(deps, client)
    r = client.get(
        "/superuser/stats/dashboard", headers=auth_headers(admin_token)
    )
    assert r.status_code == 403
