"""Regression: a recorded draft that uses cap.desktop_scroll and cap.key_send
must be grantable AND re-saveable through the public API.

Before cap.desktop_scroll / cap.key_send were registered server-side, the
recording's core loop was broken end-to-end: POST /admin/grants rejected the
refs as "unknown capability", and PATCH /workflows ran the tenant DAG validator
(registry + grants) and returned 422 on save — so a draft containing a scroll
or any allowlisted hotkey (present in virtually every real capture) could never
be granted and never be edited, even though editing is mandatory to replace the
<REPLACE_REDACTED_TEXT_n> placeholders typed text compiles to.

This walks the whole flow against a fake agent: record -> stop -> grant every
desktop capability the draft references -> replace the placeholder -> PATCH.
"""

from __future__ import annotations

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
from tests._api_helpers import auth_headers, login, seed_tenant_admin

# A capture that exercises every compiled ref: window switch, click, typed text
# (redacted), an allowlisted hotkey, and a scroll.
EVENTS = [
    {"t": 0, "kind": "window", "data": {"title": "Payroll App", "app": "payroll"}},
    {"t": 200, "kind": "click", "data": {"x": 10, "y": 20, "button": "left"}},
    {"t": 400, "kind": "text", "data": {"count": 9}},
    {"t": 600, "kind": "key", "data": {"combo": "enter"}},
    {"t": 800, "kind": "scroll", "data": {"dx": 0, "dy": -120}},
]

# The desktop capabilities a typical recording compiles to. All are remote-only
# (no secrets), so a grant carries no secret payload.
DESKTOP_CAPS = (
    "cap.window_manage",
    "cap.desktop_click",
    "cap.desktop_type",
    "cap.key_send",
    "cap.desktop_scroll",
)


def _agent_handler(events):
    def handler(task):
        action = task.inputs["action"]
        out = {"recording_id": "agent-rec-9", "status": "recording", "event_count": 0}
        if action == "status":
            out["event_count"] = len(events)
        elif action == "stop":
            out = {
                "recording_id": "agent-rec-9",
                "status": "stopped",
                "event_count": len(events),
                "events": events,
            }
        return RemoteResult(task_id=task.task_id, ok=True, outputs=out)

    return handler


def _register_agent(
    registry: AgentRegistry, tenant_id: uuid.UUID, alias: str = "lab-1"
) -> FakeAgentConnection:
    conn = FakeAgentConnection(
        AgentInfo(
            alias=alias,
            tenant_id=tenant_id,
            gui_capable=True,
            capabilities=(AgentCapability(ref=RECORDING_CAPABILITY),),
        ),
        _agent_handler(EVENTS),
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


def test_scroll_and_key_send_are_grantable(deps: AppDependencies, client: TestClient, admin_ctx) -> None:
    """The two refs added by this fix must pass /admin/grants ref-existence
    checks (admin.py rejects unknown refs with 400)."""
    _tenant, h = admin_ctx
    for ref in ("cap.desktop_scroll", "cap.key_send"):
        r = client.post(
            "/admin/grants",
            headers=h,
            json={"capability_ref": ref, "account_alias": "primary", "secrets": {}},
        )
        assert r.status_code == 201, f"{ref}: {r.text}"


def test_recorded_draft_with_scroll_and_hotkey_is_editable(
    deps: AppDependencies, client: TestClient, admin_ctx
) -> None:
    tenant, h = admin_ctx
    _register_agent(deps.agent_registry, tenant.id)

    # Record -> stop -> draft workflow lands in the list.
    rec_id = client.post(
        "/recordings", json={"name": "Payroll entry", "agent_alias": "lab-1"}, headers=h
    ).json()["recording_id"]
    stopped = client.post(f"/recordings/{rec_id}/stop", headers=h)
    assert stopped.status_code == 200, stopped.text
    stop_body = stopped.json()
    workflow_id = stop_body["workflow_id"]
    draft = stop_body["draft_dag"]
    refs = {n["ref"] for n in draft["nodes"]}
    assert {"cap.desktop_scroll", "cap.key_send"} <= refs

    # Grant every desktop capability the draft references (remote-only, no secrets).
    for ref in DESKTOP_CAPS:
        g = client.post(
            "/admin/grants",
            headers=h,
            json={"capability_ref": ref, "account_alias": "primary", "secrets": {}},
        )
        assert g.status_code == 201, f"{ref}: {g.text}"

    # The mandatory edit: replace the redacted placeholder with real text. This
    # is the save path that previously 422'd because the scroll/hotkey refs were
    # unresolvable in the tenant validator.
    for node in draft["nodes"]:
        if node["ref"] == "cap.desktop_type":
            node["inputs"]["text"] = "Acme Corp"

    r = client.patch(
        f"/workflows/{workflow_id}",
        headers=h,
        json={"dag": draft, "rationale": "filled in redacted text"},
    )
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["version"] == 2
    saved_refs = {n["ref"] for n in saved["dag"]["nodes"]}
    assert {"cap.desktop_scroll", "cap.key_send"} <= saved_refs
    assert "<REPLACE_REDACTED_TEXT_1>" not in str(saved["dag"])


def test_edit_still_blocked_when_a_referenced_cap_is_ungranted(
    deps: AppDependencies, client: TestClient, admin_ctx
) -> None:
    """The fix registers the refs but must not weaken grant enforcement: saving
    a draft whose scroll/hotkey caps are NOT granted still fails validation."""
    tenant, h = admin_ctx
    _register_agent(deps.agent_registry, tenant.id)

    rec_id = client.post(
        "/recordings", json={"name": "Payroll entry", "agent_alias": "lab-1"}, headers=h
    ).json()["recording_id"]
    stop_body = client.post(f"/recordings/{rec_id}/stop", headers=h).json()
    workflow_id = stop_body["workflow_id"]
    draft = stop_body["draft_dag"]

    # Grant everything EXCEPT the two refs under test.
    for ref in ("cap.window_manage", "cap.desktop_click", "cap.desktop_type"):
        client.post(
            "/admin/grants",
            headers=h,
            json={"capability_ref": ref, "account_alias": "primary", "secrets": {}},
        )

    r = client.patch(
        f"/workflows/{workflow_id}",
        headers=h,
        json={"dag": draft, "rationale": "x"},
    )
    assert r.status_code == 422
    assert "not granted" in r.json()["detail"]
