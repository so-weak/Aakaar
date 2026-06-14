"""Recordings REST API: authz (tenant-admin only), tenant isolation, the full
start → status → stop → draft-workflow flow against a fake agent, privacy
rejection of raw text, discard, and the concurrency bound."""

from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient

from aakaar.api import AppDependencies
from aakaar.services.recordings import RECORDING_CAPABILITY
from aakaar.workers.remote import (
    AgentCapability,
    AgentInfo,
    AgentRegistry,
    FakeAgentConnection,
    RemoteResult,
)
from tests._api_helpers import auth_headers, login, seed_tenant_admin, seed_tenant_user

EVENTS = [
    {"t": 0, "kind": "window", "data": {"title": "Payroll App", "app": "payroll"}},
    {"t": 200, "kind": "click", "data": {"x": 10, "y": 20, "button": "left"}},
    {"t": 400, "kind": "text", "data": {"count": 9}},
    {"t": 600, "kind": "key", "data": {"combo": "enter"}},
    {"t": 800, "kind": "scroll", "data": {"dx": 0, "dy": -120}},
]


def _agent_handler(events, *, truncated: bool = False):
    def handler(task):
        action = task.inputs["action"]
        out = {
            "recording_id": "agent-rec-9",
            "status": "recording",
            "event_count": 0,
            "truncated": truncated,
        }
        if action == "status":
            out["event_count"] = len(events)
        elif action == "stop":
            out = {
                "recording_id": "agent-rec-9",
                "status": "stopped",
                "event_count": len(events),
                "truncated": truncated,
                "events": events,
            }
        elif action == "discard":
            out = {"recording_id": "agent-rec-9", "status": "discarded", "event_count": 0}
        return RemoteResult(task_id=task.task_id, ok=True, outputs=out)

    return handler


def _register_agent(
    registry: AgentRegistry,
    tenant_id: uuid.UUID,
    alias: str = "lab-1",
    events=EVENTS,
    *,
    truncated: bool = False,
) -> FakeAgentConnection:
    conn = FakeAgentConnection(
        AgentInfo(
            alias=alias,
            tenant_id=tenant_id,
            gui_capable=True,
            capabilities=(AgentCapability(ref=RECORDING_CAPABILITY),),
        ),
        _agent_handler(events, truncated=truncated),
    )
    registry.register(conn)
    return conn


@pytest.fixture()
def admin_ctx(deps: AppDependencies, client: TestClient):
    tenant, _admin = seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="admin@a.test", admin_password="adminpass1"
    )
    token = login(client, email="admin@a.test", password="adminpass1")
    return tenant, auth_headers(token)


def _start(client: TestClient, headers, alias: str = "lab-1"):
    return client.post(
        "/recordings", json={"name": "Payroll entry", "agent_alias": alias}, headers=headers
    )


# ---------- authz -------------------------------------------------------------


def test_recordings_require_tenant_admin(
    deps: AppDependencies, client: TestClient, admin_ctx
) -> None:
    tenant, _ = admin_ctx
    seed_tenant_user(deps, tenant_id=tenant.id, email="user@a.test", password="userpass1")
    h = auth_headers(login(client, email="user@a.test", password="userpass1"))
    assert _start(client, h).status_code == 403
    assert client.get("/recordings", headers=h).status_code == 403
    assert client.get("/recordings/abc", headers=h).status_code == 403
    assert client.post("/recordings/abc/stop", headers=h).status_code == 403
    assert client.delete("/recordings/abc", headers=h).status_code == 403


def test_recordings_require_auth(client: TestClient) -> None:
    assert client.post("/recordings", json={"name": "x", "agent_alias": "a"}).status_code == 401


def test_cross_tenant_recording_is_404(
    deps: AppDependencies, client: TestClient, admin_ctx
) -> None:
    tenant, h = admin_ctx
    _register_agent(deps.agent_registry, tenant.id)
    rec_id = _start(client, h).json()["recording_id"]

    seed_tenant_admin(
        deps, slug="rival", name="Rival", admin_email="admin@b.test", admin_password="adminpass1"
    )
    h_b = auth_headers(login(client, email="admin@b.test", password="adminpass1"))
    assert client.get(f"/recordings/{rec_id}", headers=h_b).status_code == 404
    assert client.post(f"/recordings/{rec_id}/stop", headers=h_b).status_code == 404
    assert client.delete(f"/recordings/{rec_id}", headers=h_b).status_code == 404
    assert client.get("/recordings", headers=h_b).json() == []
    # The owner still sees it.
    assert client.get(f"/recordings/{rec_id}", headers=h).status_code == 200


# ---------- lifecycle ----------------------------------------------------------


def test_full_recording_flow_creates_draft_workflow(
    deps: AppDependencies, client: TestClient, admin_ctx
) -> None:
    tenant, h = admin_ctx
    conn = _register_agent(deps.agent_registry, tenant.id)

    r = _start(client, h)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "recording"
    assert body["agent_alias"] == "lab-1"
    assert "redacted" in body["privacy_note"]
    rec_id = body["recording_id"]
    # The server never exposes the agent's internal recording id.
    assert rec_id != "agent-rec-9"

    r = client.get(f"/recordings/{rec_id}", headers=h)
    assert r.status_code == 200
    status = r.json()
    assert status["event_count"] == len(EVENTS)
    assert status["status"] == "recording"
    assert status["duration_seconds"] >= 0

    assert [x["recording_id"] for x in client.get("/recordings", headers=h).json()] == [rec_id]

    r = client.post(f"/recordings/{rec_id}/stop", headers=h)
    assert r.status_code == 200, r.text
    stopped = r.json()
    assert stopped["status"] == "stopped"
    assert stopped["event_count"] == len(EVENTS)
    refs = [n["ref"] for n in stopped["draft_dag"]["nodes"]]
    assert refs == [
        "cap.window_manage",
        "cap.desktop_click",
        "cap.desktop_type",
        "cap.key_send",
        "cap.desktop_scroll",
    ]
    assert any("redacted" in w for w in stopped["warnings"])
    assert "<REPLACE_REDACTED_TEXT_1>" in str(stopped["draft_dag"])

    # The draft landed in the workflows list for review.
    workflows = client.get("/workflows", headers=h).json()
    assert [w["id"] for w in workflows] == [str(stopped["workflow_id"])]
    assert workflows[0]["name"] == "Payroll entry"

    # The agent saw start, status, stop — and the registry entry is gone.
    assert [t.inputs["action"] for t in conn.dispatched] == ["start", "status", "stop"]
    assert client.get(f"/recordings/{rec_id}", headers=h).status_code == 404


def test_stop_warns_when_capture_was_truncated(
    deps: AppDependencies, client: TestClient, admin_ctx
) -> None:
    # The agent auto-stopped at its event cap: the operator must be told the
    # draft is incomplete, and the status payload exposes the flag too.
    tenant, h = admin_ctx
    _register_agent(deps.agent_registry, tenant.id, truncated=True)
    rec_id = _start(client, h).json()["recording_id"]

    status = client.get(f"/recordings/{rec_id}", headers=h).json()
    assert status["truncated"] is True

    stopped = client.post(f"/recordings/{rec_id}/stop", headers=h).json()
    assert any("event limit" in w for w in stopped["warnings"])


def test_stop_rejects_raw_text_from_agent(
    deps: AppDependencies, client: TestClient, admin_ctx
) -> None:
    tenant, h = admin_ctx
    leaky = [{"t": 0, "kind": "text", "data": {"count": 7, "chars": "hunter2"}}]
    _register_agent(deps.agent_registry, tenant.id, events=leaky)

    rec_id = _start(client, h).json()["recording_id"]
    r = client.post(f"/recordings/{rec_id}/stop", headers=h)
    assert r.status_code == 502
    assert "privacy" in r.json()["detail"]
    assert "hunter2" not in r.text
    # Nothing was persisted.
    assert client.get("/workflows", headers=h).json() == []


def test_stop_with_no_compilable_events_is_422(
    deps: AppDependencies, client: TestClient, admin_ctx
) -> None:
    tenant, h = admin_ctx
    _register_agent(deps.agent_registry, tenant.id, events=[])
    rec_id = _start(client, h).json()["recording_id"]
    assert client.post(f"/recordings/{rec_id}/stop", headers=h).status_code == 422
    assert client.get("/workflows", headers=h).json() == []


def test_discard_tells_agent_and_removes_entry(
    deps: AppDependencies, client: TestClient, admin_ctx
) -> None:
    tenant, h = admin_ctx
    conn = _register_agent(deps.agent_registry, tenant.id)
    rec_id = _start(client, h).json()["recording_id"]

    assert client.delete(f"/recordings/{rec_id}", headers=h).status_code == 204
    assert conn.dispatched[-1].inputs == {"action": "discard", "recording_id": "agent-rec-9"}
    assert client.get(f"/recordings/{rec_id}", headers=h).status_code == 404
    assert client.delete(f"/recordings/{rec_id}", headers=h).status_code == 404


def test_list_endpoint_discards_expired_capture_on_agent(
    deps: AppDependencies, client: TestClient, admin_ctx
) -> None:
    # The GET /recordings list endpoint runs the opportunistic discard drain.
    # Because it is an async endpoint it executes on the event-loop thread, so
    # an entry that expires and is observed only on the list path still gets an
    # agent-side discard immediately instead of waiting for the next sweep. (A
    # sync endpoint would run in a threadpool worker with no running loop, where
    # the drain silently no-ops.)
    tenant, h = admin_ctx
    conn = _register_agent(deps.agent_registry, tenant.id)
    rec_id = _start(client, h).json()["recording_id"]

    # Force the entry past its TTL via its monotonic deadline.
    svc = client.app.state.recordings
    svc._entries[rec_id].deadline = svc._clock() - 1.0

    assert client.get("/recordings", headers=h).json() == []

    # The drain runs on the event loop after the request returns; poll briefly.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if any(t.inputs["action"] == "discard" for t in conn.dispatched):
            break
        time.sleep(0.02)
    assert conn.dispatched[-1].inputs == {"action": "discard", "recording_id": "agent-rec-9"}


def test_start_without_online_agent_is_409(client: TestClient, admin_ctx) -> None:
    _, h = admin_ctx
    r = _start(client, h)
    assert r.status_code == 409
    assert "agent" in r.json()["detail"]


def test_concurrent_recording_limit(
    deps: AppDependencies, client: TestClient, admin_ctx
) -> None:
    tenant, h = admin_ctx
    _register_agent(deps.agent_registry, tenant.id)
    for _ in range(5):
        assert _start(client, h).status_code == 201
    r = _start(client, h)
    assert r.status_code == 409
    assert "limit" in r.json()["detail"]


def test_max_events_is_validated_and_forwarded(
    deps: AppDependencies, client: TestClient, admin_ctx
) -> None:
    tenant, h = admin_ctx
    conn = _register_agent(deps.agent_registry, tenant.id)
    r = client.post(
        "/recordings",
        json={"name": "n", "agent_alias": "lab-1", "max_events": 9999},
        headers=h,
    )
    assert r.status_code == 422  # above the 5000 cap
    r = client.post(
        "/recordings",
        json={"name": "n", "agent_alias": "lab-1", "max_events": 5000},
        headers=h,
    )
    assert r.status_code == 201
    assert conn.dispatched[-1].inputs == {"action": "start", "max_events": 5000}
