"""End-to-end /runs tests: start, watch, respond, list."""

from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from aakar.api.deps import AppDependencies
from aakar.shared.dag.types import Dag, Edge, Node, NodeKind
from tests._api_helpers import (
    auth_headers,
    login,
    seed_tenant_admin,
    seed_tenant_user,
)


def _save_workflow(client: TestClient, token: str, dag: Dag) -> str:
    r = client.post(
        "/workflows",
        headers=auth_headers(token),
        json={"name": "demo", "description": "", "dag": dag.model_dump(by_alias=True)},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _wait_for_status(
    client: TestClient, token: str, run_id: str, target: set[str], timeout: float = 5.0
) -> dict:
    start = time.time()
    while time.time() - start < timeout:
        r = client.get(f"/runs/{run_id}", headers=auth_headers(token))
        body = r.json()
        if body["run"]["status"] in target:
            return body
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach {target} in {timeout}s; last={body}")


def test_run_succeeds_with_control_wait(deps: AppDependencies, client: TestClient) -> None:
    tenant, _ = seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="a@a.test", admin_password="adminpass1"
    )
    seed_tenant_user(deps, tenant_id=tenant.id, email="u@a.test", password="userpass1")
    token = login(client, email="u@a.test", password="userpass1")

    dag = Dag(
        nodes=[
            Node(id="w", kind=NodeKind.CONTROL, ref="control.wait", inputs={"seconds": 0.01}),
        ]
    )
    wf_id = _save_workflow(client, token, dag)

    r = client.post(
        f"/workflows/{wf_id}/runs",
        headers=auth_headers(token),
        json={"inputs": {}},
    )
    assert r.status_code == 201, r.text
    run_id = r.json()["id"]

    body = _wait_for_status(client, token, run_id, {"succeeded"})
    statuses = [e["kind"] for e in body["events"]]
    assert "node_started" in statuses
    assert "node_completed" in statuses


def test_run_records_failure(deps: AppDependencies, client: TestClient) -> None:
    tenant, _ = seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="a@a.test", admin_password="adminpass1"
    )
    seed_tenant_user(deps, tenant_id=tenant.id, email="u@a.test", password="userpass1")
    token = login(client, email="u@a.test", password="userpass1")

    # Build a DAG referencing http.request against a definitely-broken URL.
    dag = Dag(
        nodes=[
            Node(
                id="bad",
                kind=NodeKind.ACTION,
                ref="http.request",
                inputs={"method": "GET", "url": "http://127.0.0.1:1/never", "timeout_ms": 200},
            )
        ]
    )
    wf_id = _save_workflow(client, token, dag)
    r = client.post(f"/workflows/{wf_id}/runs", headers=auth_headers(token), json={})
    run_id = r.json()["id"]

    body = _wait_for_status(client, token, run_id, {"failed"})
    assert body["run"]["error"] is not None


def test_human_prompt_pause_and_respond(deps: AppDependencies, client: TestClient) -> None:
    tenant, _ = seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="a@a.test", admin_password="adminpass1"
    )
    seed_tenant_user(deps, tenant_id=tenant.id, email="u@a.test", password="userpass1")
    token = login(client, email="u@a.test", password="userpass1")

    dag = Dag(
        nodes=[
            Node(
                id="ask",
                kind=NodeKind.CONTROL,
                ref="human.prompt",
                inputs={"message": "what's the OTP?", "expects": "otp", "timeout_seconds": 5},
            )
        ]
    )
    wf_id = _save_workflow(client, token, dag)
    r = client.post(f"/workflows/{wf_id}/runs", headers=auth_headers(token), json={})
    run_id = r.json()["id"]

    # Wait for the prompt to register.
    pending = []
    for _ in range(50):
        body = client.get(f"/runs/{run_id}", headers=auth_headers(token)).json()
        pending = body["pending_prompts"]
        if pending:
            break
        time.sleep(0.05)
    assert pending, f"no pending prompt; last events: {body.get('events', [])}"

    r = client.post(
        f"/runs/{run_id}/respond",
        headers=auth_headers(token),
        json={"node_id": "ask", "response": "123456"},
    )
    assert r.status_code == 204, r.text

    body = _wait_for_status(client, token, run_id, {"succeeded"})
    # OTP value must NOT appear in any event payload.
    for evt in body["events"]:
        assert "123456" not in str(evt["payload"])


def test_only_starter_can_respond(deps: AppDependencies, client: TestClient) -> None:
    tenant, _ = seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="a@a.test", admin_password="adminpass1"
    )
    seed_tenant_user(deps, tenant_id=tenant.id, email="starter@a.test", password="starterpass1")
    seed_tenant_user(deps, tenant_id=tenant.id, email="other@a.test", password="otherpass1")

    starter_token = login(client, email="starter@a.test", password="starterpass1")
    other_token = login(client, email="other@a.test", password="otherpass1")

    dag = Dag(
        nodes=[
            Node(
                id="ask",
                kind=NodeKind.CONTROL,
                ref="human.prompt",
                inputs={"message": "?", "timeout_seconds": 5},
            )
        ]
    )
    wf_id = _save_workflow(client, starter_token, dag)
    run_id = client.post(
        f"/workflows/{wf_id}/runs", headers=auth_headers(starter_token), json={}
    ).json()["id"]

    # Wait for prompt to register.
    for _ in range(50):
        body = client.get(f"/runs/{run_id}", headers=auth_headers(starter_token)).json()
        if body["pending_prompts"]:
            break
        time.sleep(0.05)

    r = client.post(
        f"/runs/{run_id}/respond",
        headers=auth_headers(other_token),
        json={"node_id": "ask", "response": "x"},
    )
    assert r.status_code == 403

    # Starter can resolve, freeing the run from the test.
    client.post(
        f"/runs/{run_id}/respond",
        headers=auth_headers(starter_token),
        json={"node_id": "ask", "response": "x"},
    )
    _wait_for_status(client, starter_token, run_id, {"succeeded"})


# Quiet a lint about asyncio import — used in conftest, kept here for clarity.
_ = asyncio
