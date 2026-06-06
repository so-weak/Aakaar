"""Remote execution: placement validation, agent registry/resolution, the
RemoteDispatcher driving the executor end-to-end via an in-process fake agent,
retry/failure propagation, placement pre-flight, and the agents REST API."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from aakaar.api import AppDependencies
from aakaar.interpreter import LocalExecutor, RunContext
from aakaar.interpreter.activities.registry import ActivityRegistry
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.interpreter.events import InMemoryEventRecorder
from aakaar.interpreter.signals import SignalHub
from aakaar.shared.dag.types import Dag, Edge, Node, NodeKind, RetrySpec
from aakaar.shared.dag.validator import ValidationError, validate_dag
from aakaar.shared.registry import build_default_registry
from aakaar.workers.remote import (
    AgentCapability,
    AgentInfo,
    AgentRegistry,
    FakeAgentConnection,
    NoAgentAvailable,
    RemoteDispatcher,
    RemoteResult,
    check_placement,
)
from tests._api_helpers import auth_headers, login, seed_tenant_admin

TENANT = uuid.uuid4()


def _agent(alias: str, *, caps: list[str], gui: bool = False, pools=(), tenant=TENANT) -> AgentInfo:
    return AgentInfo(
        alias=alias,
        tenant_id=tenant,
        os="linux",
        gui_capable=gui,
        version="1",
        pools=tuple(pools),
        capabilities=tuple(AgentCapability(ref=c) for c in caps),
    )


def _fake(alias: str, handler, **kw) -> FakeAgentConnection:
    return FakeAgentConnection(_agent(alias, **kw), handler)


def _run_ctx(tenant=TENANT) -> RunContext:
    rid = uuid.uuid4()
    actx = ActivityContext(
        tenant_id=tenant,
        run_id=rid,
        registry=build_default_registry(),
        object_store=None,  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
    )
    return RunContext(run_id=rid, tenant_id=tenant, activity_ctx=actx)


# ---------- placement validation -------------------------------------------


def test_validator_rejects_remote_control_node() -> None:
    dag = Dag(nodes=[Node(id="w", kind=NodeKind.CONTROL, ref="control.wait", target="lab")])
    with pytest.raises(ValidationError, match="control node"):
        validate_dag(dag)


def test_validator_allows_remote_capability_node() -> None:
    dag = Dag(
        nodes=[Node(id="a", kind=NodeKind.ACTION, ref="x.remote_op", target="branch-ops")]
    )
    validate_dag(dag)  # structural only; no raise


# ---------- registry + resolution ------------------------------------------


def test_registry_resolves_by_alias_and_pool() -> None:
    reg = AgentRegistry()
    reg.register(_fake("lab-1", lambda t: None, caps=["x.op"], pools=["branch"]))
    assert reg.resolve(TENANT, "lab-1", ref="x.op").info.alias == "lab-1"
    assert reg.resolve(TENANT, "branch", ref="x.op").info.alias == "lab-1"


def test_registry_resolution_failures() -> None:
    reg = AgentRegistry()
    reg.register(_fake("lab-1", lambda t: None, caps=["x.op"], gui=False, pools=["branch"]))
    with pytest.raises(NoAgentAvailable, match="no online agent"):
        reg.resolve(TENANT, "missing-pool", ref="x.op")
    with pytest.raises(NoAgentAvailable, match="support capability"):
        reg.resolve(TENANT, "lab-1", ref="other.op")
    with pytest.raises(NoAgentAvailable, match="GUI session"):
        reg.resolve(TENANT, "lab-1", ref="x.op", require_gui=True)


def test_registry_is_tenant_scoped() -> None:
    reg = AgentRegistry()
    reg.register(_fake("lab-1", lambda t: None, caps=["x.op"]))
    other = uuid.uuid4()
    with pytest.raises(NoAgentAvailable):
        reg.resolve(other, "lab-1", ref="x.op")


# ---------- executor end-to-end via a fake agent ---------------------------


@pytest.mark.asyncio
async def test_executor_dispatches_remote_node_to_agent() -> None:
    reg = AgentRegistry()

    def handler(task):
        return RemoteResult(task_id=task.task_id, ok=True, outputs={"echo": task.inputs})

    conn = _fake("lab-1", handler, caps=["x.remote_op"])
    reg.register(conn)
    dispatcher = RemoteDispatcher(agents=reg, registry=None, audit=None)

    # No local handler registered for x.remote_op — proves it ran remotely.
    ex = LocalExecutor(
        activities=ActivityRegistry(),
        recorder=InMemoryEventRecorder(),
        signals=SignalHub(),
        remote_dispatcher=dispatcher,
    )
    dag = Dag(
        nodes=[
            Node(
                id="n",
                kind=NodeKind.ACTION,
                ref="x.remote_op",
                target="lab-1",
                inputs={"a": 1},
            )
        ]
    )
    outcome = await ex.execute(dag, _run_ctx())
    assert outcome.status == "succeeded"
    assert outcome.outputs["n"] == {"echo": {"a": 1}}
    assert len(conn.dispatched) == 1
    assert conn.dispatched[0].ref == "x.remote_op"


@pytest.mark.asyncio
async def test_remote_failure_retries_then_fails() -> None:
    reg = AgentRegistry()
    calls = {"n": 0}

    def handler(task):
        calls["n"] += 1
        return RemoteResult(task_id=task.task_id, ok=False, error={"message": "boom"})

    reg.register(_fake("lab-1", handler, caps=["x.op"]))
    ex = LocalExecutor(
        activities=ActivityRegistry(),
        recorder=InMemoryEventRecorder(),
        signals=SignalHub(),
        remote_dispatcher=RemoteDispatcher(agents=reg, registry=None, audit=None),
    )
    dag = Dag(
        nodes=[
            Node(
                id="n",
                kind=NodeKind.ACTION,
                ref="x.op",
                target="lab-1",
                retry=RetrySpec(max_attempts=2, backoff_ms=0),
            )
        ]
    )
    outcome = await ex.execute(dag, _run_ctx())
    assert outcome.status == "failed"
    assert calls["n"] == 2  # retried once


@pytest.mark.asyncio
async def test_remote_node_without_agent_fails_clearly() -> None:
    ex = LocalExecutor(
        activities=ActivityRegistry(),
        recorder=InMemoryEventRecorder(),
        signals=SignalHub(),
        remote_dispatcher=RemoteDispatcher(agents=AgentRegistry(), registry=None, audit=None),
    )
    dag = Dag(nodes=[Node(id="n", kind=NodeKind.ACTION, ref="x.op", target="lab-1")])
    outcome = await ex.execute(dag, _run_ctx())
    assert outcome.status == "failed"
    assert "cannot be placed" in (outcome.error or {}).get("message", "")


@pytest.mark.asyncio
async def test_mixed_local_and_remote_run() -> None:
    reg = AgentRegistry()
    reg.register(
        _fake(
            "lab-1",
            lambda t: RemoteResult(task_id=t.task_id, ok=True, outputs={"remote": True}),
            caps=["x.remote_op"],
        )
    )
    acts = ActivityRegistry()

    async def local_op(_ctx, _inputs):
        return {"local": True}

    acts.register("x.local_op", local_op)
    ex = LocalExecutor(
        activities=acts,
        recorder=InMemoryEventRecorder(),
        signals=SignalHub(),
        remote_dispatcher=RemoteDispatcher(agents=reg, registry=None, audit=None),
    )
    dag = Dag(
        nodes=[
            Node(id="l", kind=NodeKind.ACTION, ref="x.local_op"),
            Node(id="r", kind=NodeKind.ACTION, ref="x.remote_op", target="lab-1"),
        ],
        edges=[Edge.model_validate({"from": "l", "to": "r"})],
    )
    outcome = await ex.execute(dag, _run_ctx())
    assert outcome.status == "succeeded"
    assert outcome.outputs["l"] == {"local": True}
    assert outcome.outputs["r"] == {"remote": True}


# ---------- placement pre-flight -------------------------------------------


def test_check_placement_reports_and_clears() -> None:
    dag = Dag(nodes=[Node(id="n", kind=NodeKind.ACTION, ref="x.op", target="lab-1")])
    empty = AgentRegistry()
    issues = check_placement(dag, TENANT, agents=empty, registry=None)
    assert len(issues) == 1 and issues[0]["node_id"] == "n"

    reg = AgentRegistry()
    reg.register(_fake("lab-1", lambda t: None, caps=["x.op"]))
    assert check_placement(dag, TENANT, agents=reg, registry=None) == []


# ---------- agents REST API ------------------------------------------------


def test_agents_rest_enroll_list_revoke(deps: AppDependencies, client: TestClient) -> None:
    seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="admin@a.test", admin_password="adminpass1"
    )
    tok = login(client, email="admin@a.test", password="adminpass1")
    h = auth_headers(tok)

    r = client.post("/agents/enroll", json={"alias": "lab-win-03", "pools": ["branch-ops"]}, headers=h)
    assert r.status_code == 201, r.text
    body = r.json()
    assert "." in body["enrollment_key"]  # "<agent_id>.<secret>"
    agent_id = body["id"]

    r = client.get("/agents", headers=h)
    assert r.status_code == 200
    agents = r.json()
    assert len(agents) == 1
    assert agents[0]["alias"] == "lab-win-03"
    assert agents[0]["online"] is False
    assert agents[0]["status"] == "enrolled"

    # Duplicate alias -> 409
    r = client.post("/agents/enroll", json={"alias": "lab-win-03"}, headers=h)
    assert r.status_code == 409

    r = client.delete(f"/agents/{agent_id}", headers=h)
    assert r.status_code == 204
    assert client.get("/agents", headers=h).json() == []


def test_placement_check_endpoint(deps: AppDependencies, client: TestClient) -> None:
    seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="admin@a.test", admin_password="adminpass1"
    )
    tok = login(client, email="admin@a.test", password="adminpass1")
    dag = {"nodes": [{"id": "n", "kind": "action", "ref": "x.op", "target": "lab-1"}], "edges": []}
    r = client.post("/placement/check", json=dag, headers=auth_headers(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["online_agents"] == 0
    assert len(body["issues"]) == 1 and body["issues"][0]["node_id"] == "n"


def test_agent_ws_connect_register_and_online(deps: AppDependencies, client: TestClient) -> None:
    """Real-WebSocket round trip: an enrolled agent connects, says hello, is
    registered, and shows online — then offline after disconnect."""
    seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="admin@a.test", admin_password="adminpass1"
    )
    tok = login(client, email="admin@a.test", password="adminpass1")
    h = auth_headers(tok)
    key = client.post(
        "/agents/enroll", json={"alias": "lab-1", "pools": ["branch"]}, headers=h
    ).json()["enrollment_key"]

    with client.websocket_connect("/ws/agents", headers={"x-agent-key": key}) as ws:
        ws.send_json(
            {
                "type": "hello",
                "os": "linux",
                "gui": False,
                "version": "1",
                "capabilities": [{"ref": "cap.shell_exec", "version": "1"}],
            }
        )
        welcome = ws.receive_json()  # sync point: server has registered us
        assert welcome["type"] == "welcome"
        agents = client.get("/agents", headers=h).json()
        assert agents[0]["online"] is True
        assert agents[0]["os"] == "linux"

    # After the socket closes the agent is marked offline.
    agents = client.get("/agents", headers=h).json()
    assert agents[0]["online"] is False


def test_agent_ws_rejects_bad_key(deps: AppDependencies, client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/ws/agents", headers={"x-agent-key": "00000000-0000-0000-0000-000000000000.nope"}
        ) as ws:
            ws.receive_json()


@pytest.mark.asyncio
async def test_run_target_overrides_node_placement() -> None:
    """A run-level target sends the whole run to an agent even when nodes carry
    no target of their own."""
    reg = AgentRegistry()
    reg.register(
        _fake(
            "lab-1",
            lambda t: RemoteResult(task_id=t.task_id, ok=True, outputs={"via": "agent"}),
            caps=["x.op"],
        )
    )
    ex = LocalExecutor(
        activities=ActivityRegistry(),
        recorder=InMemoryEventRecorder(),
        signals=SignalHub(),
        remote_dispatcher=RemoteDispatcher(agents=reg, registry=None, audit=None),
    )
    dag = Dag(nodes=[Node(id="n", kind=NodeKind.ACTION, ref="x.op")])  # no per-node target
    rid = uuid.uuid4()
    actx = ActivityContext(
        tenant_id=TENANT, run_id=rid, registry=build_default_registry(),
        object_store=None, vault=None,  # type: ignore[arg-type]
    )
    ctx = RunContext(run_id=rid, tenant_id=TENANT, activity_ctx=actx, run_target="lab-1")
    outcome = await ex.execute(dag, ctx)
    assert outcome.status == "succeeded"
    assert outcome.outputs["n"] == {"via": "agent"}


@pytest.mark.asyncio
async def test_run_target_server_forces_local() -> None:
    """run_target='server' runs everything on the host even when a node targets
    an agent."""
    reg = AgentRegistry()
    fake = _fake(
        "lab-1",
        lambda t: RemoteResult(task_id=t.task_id, ok=True, outputs={"via": "agent"}),
        caps=["x.op"],
    )
    reg.register(fake)
    acts = ActivityRegistry()

    async def local_op(_c, _i):
        return {"via": "server"}

    acts.register("x.op", local_op)
    ex = LocalExecutor(
        activities=acts,
        recorder=InMemoryEventRecorder(),
        signals=SignalHub(),
        remote_dispatcher=RemoteDispatcher(agents=reg, registry=None, audit=None),
    )
    dag = Dag(nodes=[Node(id="n", kind=NodeKind.ACTION, ref="x.op", target="lab-1")])
    rid = uuid.uuid4()
    actx = ActivityContext(
        tenant_id=TENANT, run_id=rid, registry=build_default_registry(),
        object_store=None, vault=None,  # type: ignore[arg-type]
    )
    ctx = RunContext(run_id=rid, tenant_id=TENANT, activity_ctx=actx, run_target="server")
    outcome = await ex.execute(dag, ctx)
    assert outcome.status == "succeeded"
    assert outcome.outputs["n"] == {"via": "server"}
    assert fake.dispatched == []  # never went to the agent
