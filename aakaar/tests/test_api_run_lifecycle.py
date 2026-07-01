"""End-to-end run lifecycle tests: pause, resume, cancel, rerun.

Determinism notes: multi-layer DAGs are built from control nodes —
human.prompt acts as an explicit synchronization point (the test controls
exactly when a layer finishes via /respond), and long control.wait nodes
stand in for slow capability work that a cancel must interrupt.
"""

from __future__ import annotations

import time
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from aakaar.api.deps import AppDependencies
from aakaar.db.models import AuditLog, Run
from aakaar.shared.dag.types import Dag, Edge, Node, NodeKind
from tests._api_helpers import (
    auth_headers,
    login,
    seed_superuser,
    seed_tenant_admin,
    seed_tenant_user,
)


def _save_workflow(client: TestClient, token: str, dag: Dag, name: str = "demo") -> str:
    r = client.post(
        "/workflows",
        headers=auth_headers(token),
        json={"name": name, "description": "", "dag": dag.model_dump(by_alias=True)},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _start_run(client: TestClient, token: str, wf_id: str, **body) -> str:
    r = client.post(f"/workflows/{wf_id}/runs", headers=auth_headers(token), json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _wait_for_status(
    client: TestClient, token: str, run_id: str, target: set[str], timeout: float = 5.0
) -> dict:
    start = time.time()
    while time.time() - start < timeout:
        body = client.get(f"/runs/{run_id}", headers=auth_headers(token)).json()
        if body["run"]["status"] in target:
            return body
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach {target} in {timeout}s; last={body}")


def _wait_for_prompt(
    client: TestClient, token: str, run_id: str, node_id: str, timeout: float = 5.0
) -> dict:
    start = time.time()
    while time.time() - start < timeout:
        body = client.get(f"/runs/{run_id}", headers=auth_headers(token)).json()
        if any(p["node_id"] == node_id for p in body["pending_prompts"]):
            return body
        time.sleep(0.05)
    raise AssertionError(f"prompt {node_id} never registered; last={body}")


def _prompt_node(node_id: str, message: str = "?") -> Node:
    return Node(
        id=node_id,
        kind=NodeKind.CONTROL,
        ref="human.prompt",
        inputs={"message": message, "timeout_seconds": 30},
    )


def _two_layer_prompt_dag() -> Dag:
    return Dag(
        nodes=[_prompt_node("p1"), _prompt_node("p2")],
        edges=[Edge(source="p1", target="p2")],
    )


def _long_wait_dag() -> Dag:
    return Dag(
        nodes=[Node(id="slow", kind=NodeKind.CONTROL, ref="control.wait", inputs={"seconds": 30})]
    )


def _seed(deps: AppDependencies, client: TestClient, slug: str = "acme") -> tuple:
    tenant, _ = seed_tenant_admin(
        deps,
        slug=slug,
        name=slug.title(),
        admin_email=f"a@{slug}.test",
        admin_password="adminpass1",
    )
    seed_tenant_user(deps, tenant_id=tenant.id, email=f"u@{slug}.test", password="userpass1")
    admin_token = login(client, email=f"a@{slug}.test", password="adminpass1")
    user_token = login(client, email=f"u@{slug}.test", password="userpass1")
    return tenant, admin_token, user_token


# ---------- pause / resume --------------------------------------------------


def test_pause_blocks_next_layer_and_resume_completes(
    deps: AppDependencies, client: TestClient
) -> None:
    _, _, token = _seed(deps, client)
    wf_id = _save_workflow(client, token, _two_layer_prompt_dag())
    run_id = _start_run(client, token, wf_id)
    _wait_for_prompt(client, token, run_id, "p1")

    r = client.post(f"/runs/{run_id}/pause", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "paused"

    # Pausing twice is a conflict.
    r = client.post(f"/runs/{run_id}/pause", headers=auth_headers(token))
    assert r.status_code == 409

    # Finish layer 1: the pending prompt can still be answered while paused.
    r = client.post(
        f"/runs/{run_id}/respond",
        headers=auth_headers(token),
        json={"node_id": "p1", "response": "ok"},
    )
    assert r.status_code == 204, r.text

    # Layer 2 (p2) must NOT start while the operator pause holds.
    time.sleep(0.4)
    body = client.get(f"/runs/{run_id}", headers=auth_headers(token)).json()
    assert body["run"]["status"] == "paused"
    assert body["pending_prompts"] == []
    started = [e["node_id"] for e in body["events"] if e["kind"] == "node_started"]
    assert "p2" not in started

    r = client.post(f"/runs/{run_id}/resume", headers=auth_headers(token))
    assert r.status_code == 200, r.text

    _wait_for_prompt(client, token, run_id, "p2")
    client.post(
        f"/runs/{run_id}/respond",
        headers=auth_headers(token),
        json={"node_id": "p2", "response": "ok"},
    )
    body = _wait_for_status(client, token, run_id, {"succeeded"})

    pauses = [e for e in body["events"] if e["kind"] == "run_paused"]
    resumes = [e for e in body["events"] if e["kind"] == "run_resumed"]
    assert any(e["payload"].get("reason") == "operator" for e in pauses)
    assert any(e["payload"].get("reason") == "operator" for e in resumes)


def test_resume_does_not_release_human_prompt_wait(
    deps: AppDependencies, client: TestClient
) -> None:
    _, _, token = _seed(deps, client)
    wf_id = _save_workflow(client, token, Dag(nodes=[_prompt_node("ask")]))
    run_id = _start_run(client, token, wf_id)
    _wait_for_prompt(client, token, run_id, "ask")

    # The run is prompt-paused, not operator-paused: resume must refuse.
    r = client.post(f"/runs/{run_id}/resume", headers=auth_headers(token))
    assert r.status_code == 409, r.text
    assert "human prompt" in r.json()["detail"]

    # The prompt is still pending; answering it completes the run.
    body = client.get(f"/runs/{run_id}", headers=auth_headers(token)).json()
    assert [p["node_id"] for p in body["pending_prompts"]] == ["ask"]
    client.post(
        f"/runs/{run_id}/respond",
        headers=auth_headers(token),
        json={"node_id": "ask", "response": "ok"},
    )
    _wait_for_status(client, token, run_id, {"succeeded"})


# ---------- cancel ----------------------------------------------------------


def test_cancel_interrupts_long_wait(deps: AppDependencies, client: TestClient) -> None:
    _, _, token = _seed(deps, client)
    wf_id = _save_workflow(client, token, _long_wait_dag())
    run_id = _start_run(client, token, wf_id)
    _wait_for_status(client, token, run_id, {"running"})

    # Not paused — resume is a conflict.
    r = client.post(f"/runs/{run_id}/resume", headers=auth_headers(token))
    assert r.status_code == 409
    assert r.json()["detail"] == "run is not paused"

    r = client.post(f"/runs/{run_id}/cancel", headers=auth_headers(token))
    assert r.status_code == 200, r.text

    # The 30s control.wait must be interrupted, not waited out.
    body = _wait_for_status(client, token, run_id, {"cancelled"})
    assert body["run"]["ended_at"] is not None
    assert "run_cancelled" in [e["kind"] for e in body["events"]]
    # Cancellation is not a node failure.
    assert "node_failed" not in [e["kind"] for e in body["events"]]

    # Terminal runs reject further lifecycle mutations.
    assert client.post(f"/runs/{run_id}/cancel", headers=auth_headers(token)).status_code == 409
    assert client.post(f"/runs/{run_id}/pause", headers=auth_headers(token)).status_code == 409
    assert client.post(f"/runs/{run_id}/resume", headers=auth_headers(token)).status_code == 409


def test_cancel_releases_pending_prompt(deps: AppDependencies, client: TestClient) -> None:
    _, _, token = _seed(deps, client)
    wf_id = _save_workflow(client, token, Dag(nodes=[_prompt_node("ask")]))
    run_id = _start_run(client, token, wf_id)
    _wait_for_prompt(client, token, run_id, "ask")

    r = client.post(f"/runs/{run_id}/cancel", headers=auth_headers(token))
    assert r.status_code == 200, r.text

    body = _wait_for_status(client, token, run_id, {"cancelled"})
    assert body["pending_prompts"] == []
    r = client.post(
        f"/runs/{run_id}/respond",
        headers=auth_headers(token),
        json={"node_id": "ask", "response": "late"},
    )
    assert r.status_code == 409


def test_cancel_of_paused_run_unwinds(deps: AppDependencies, client: TestClient) -> None:
    _, _, token = _seed(deps, client)
    wf_id = _save_workflow(client, token, _two_layer_prompt_dag())
    run_id = _start_run(client, token, wf_id)
    _wait_for_prompt(client, token, run_id, "p1")

    assert client.post(f"/runs/{run_id}/pause", headers=auth_headers(token)).status_code == 200
    client.post(
        f"/runs/{run_id}/respond",
        headers=auth_headers(token),
        json={"node_id": "p1", "response": "ok"},
    )
    # Run now sits at the layer gate; cancel must free and end it.
    assert client.post(f"/runs/{run_id}/cancel", headers=auth_headers(token)).status_code == 200
    _wait_for_status(client, token, run_id, {"cancelled"})


# ---------- rerun -----------------------------------------------------------


def test_rerun_pins_original_version_and_inputs(
    deps: AppDependencies, client: TestClient
) -> None:
    _, _, token = _seed(deps, client)
    dag_v1 = Dag(
        nodes=[Node(id="v_one", kind=NodeKind.CONTROL, ref="control.wait", inputs={"seconds": 0.01})]
    )
    wf_id = _save_workflow(client, token, dag_v1)
    source_id = _start_run(client, token, wf_id, inputs={"foo": "bar"})
    _wait_for_status(client, token, source_id, {"succeeded"})

    # Publish version 2 with a different node id.
    dag_v2 = Dag(
        nodes=[Node(id="v_two", kind=NodeKind.CONTROL, ref="control.wait", inputs={"seconds": 0.01})]
    )
    r = client.patch(
        f"/workflows/{wf_id}",
        headers=auth_headers(token),
        json={"dag": dag_v2.model_dump(by_alias=True)},
    )
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2

    r = client.post(f"/runs/{source_id}/rerun", headers=auth_headers(token))
    assert r.status_code == 201, r.text
    rerun = r.json()
    assert rerun["id"] != source_id
    assert rerun["workflow_version"] == 1  # pinned, despite v2 being latest

    body = _wait_for_status(client, token, rerun["id"], {"succeeded"})
    started = [e["node_id"] for e in body["events"] if e["kind"] == "node_started"]
    assert started == ["v_one"]  # executed the v1 DAG, not v2

    with deps.session_factory.session() as s:
        row = s.get(Run, uuid.UUID(rerun["id"]))
        assert row is not None
        assert row.inputs == {"foo": "bar"}  # original inputs carried over


def test_rerun_requires_terminal_run(deps: AppDependencies, client: TestClient) -> None:
    _, _, token = _seed(deps, client)
    wf_id = _save_workflow(client, token, Dag(nodes=[_prompt_node("ask")]))
    run_id = _start_run(client, token, wf_id)
    _wait_for_prompt(client, token, run_id, "ask")

    r = client.post(f"/runs/{run_id}/rerun", headers=auth_headers(token))
    assert r.status_code == 409

    client.post(
        f"/runs/{run_id}/respond",
        headers=auth_headers(token),
        json={"node_id": "ask", "response": "ok"},
    )
    _wait_for_status(client, token, run_id, {"succeeded"})


# ---------- authz -----------------------------------------------------------


def test_cross_tenant_lifecycle_is_404(deps: AppDependencies, client: TestClient) -> None:
    _, _, token_a = _seed(deps, client, slug="acme")
    _, _, token_b = _seed(deps, client, slug="umbra")

    wf_id = _save_workflow(client, token_a, Dag(nodes=[_prompt_node("ask")]))
    run_id = _start_run(client, token_a, wf_id)
    _wait_for_prompt(client, token_a, run_id, "ask")

    for action in ("pause", "resume", "cancel", "rerun"):
        r = client.post(f"/runs/{run_id}/{action}", headers=auth_headers(token_b))
        assert r.status_code == 404, f"{action}: {r.status_code} {r.text}"

    client.post(
        f"/runs/{run_id}/respond",
        headers=auth_headers(token_a),
        json={"node_id": "ask", "response": "ok"},
    )
    _wait_for_status(client, token_a, run_id, {"succeeded"})


def test_lifecycle_requires_starter_or_admin(
    deps: AppDependencies, client: TestClient
) -> None:
    tenant, admin_token, starter_token = _seed(deps, client)
    seed_tenant_user(deps, tenant_id=tenant.id, email="other@acme.test", password="otherpass1")
    other_token = login(client, email="other@acme.test", password="otherpass1")

    wf_id = _save_workflow(client, starter_token, Dag(nodes=[_prompt_node("ask")]))
    run_id = _start_run(client, starter_token, wf_id)
    _wait_for_prompt(client, starter_token, run_id, "ask")

    # A non-starter tenant user may not control the run; a tenant admin may.
    assert client.post(f"/runs/{run_id}/pause", headers=auth_headers(other_token)).status_code == 403
    assert client.post(f"/runs/{run_id}/pause", headers=auth_headers(admin_token)).status_code == 200
    assert client.post(f"/runs/{run_id}/resume", headers=auth_headers(other_token)).status_code == 403
    assert client.post(f"/runs/{run_id}/resume", headers=auth_headers(admin_token)).status_code == 200

    client.post(
        f"/runs/{run_id}/respond",
        headers=auth_headers(starter_token),
        json={"node_id": "ask", "response": "ok"},
    )
    _wait_for_status(client, starter_token, run_id, {"succeeded"})

    with deps.session_factory.session() as s:
        actions = set(
            s.scalars(
                select(AuditLog.action).where(AuditLog.target_id == str(run_id))
            ).all()
        )
    assert {"run.start", "run.pause", "run.resume"} <= actions


# ---------- superuser cross-tenant controls ---------------------------------


def test_superuser_can_pause_resume_cancel_any_run(
    deps: AppDependencies, client: TestClient
) -> None:
    """A platform superuser can drive another tenant's run's lifecycle from
    the operator console — no owner/tenant check, unlike the tenant routes."""
    _, _, token = _seed(deps, client)
    wf_id = _save_workflow(client, token, _two_layer_prompt_dag())
    run_id = _start_run(client, token, wf_id)
    _wait_for_prompt(client, token, run_id, "p1")

    seed_superuser(deps, email="root@ops.test", password="rootpass1")
    su = login(client, email="root@ops.test", password="rootpass1")

    # Pause as superuser → 200 + paused; a second pause conflicts.
    r = client.post(f"/superuser/runs/{run_id}/pause", headers=auth_headers(su))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "paused"
    assert client.post(f"/superuser/runs/{run_id}/pause", headers=auth_headers(su)).status_code == 409

    # Resume, then cancel — both cross-tenant.
    assert client.post(f"/superuser/runs/{run_id}/resume", headers=auth_headers(su)).status_code == 200
    assert client.post(f"/superuser/runs/{run_id}/cancel", headers=auth_headers(su)).status_code == 200
    _wait_for_status(client, token, run_id, {"cancelled"})

    # Controlling a finished run is a conflict.
    assert client.post(f"/superuser/runs/{run_id}/cancel", headers=auth_headers(su)).status_code == 409


def test_superuser_run_control_requires_superuser_and_real_run(
    deps: AppDependencies, client: TestClient
) -> None:
    """Non-superusers are rejected; an unknown run is 404."""
    _, admin_token, _ = _seed(deps, client)
    seed_superuser(deps, email="root2@ops.test", password="rootpass1")
    su = login(client, email="root2@ops.test", password="rootpass1")

    # Tenant admin is not a superuser → blocked by the router dependency.
    assert client.post(
        f"/superuser/runs/{uuid.uuid4()}/pause", headers=auth_headers(admin_token)
    ).status_code == 403
    # Superuser, but the run doesn't exist → 404.
    assert client.post(
        f"/superuser/runs/{uuid.uuid4()}/cancel", headers=auth_headers(su)
    ).status_code == 404
