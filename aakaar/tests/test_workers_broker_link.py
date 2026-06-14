"""API-side master link to the rendezvous broker (workers/remote/broker_link.py):
a stub broker on a loopback port drives the link through end-to-end agent-key
verification, hello/registration, task dispatch, event relay, session teardown,
reconnect-with-backoff, and the fail-closed create_app / load_settings wiring."""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
import websockets
from fastapi.testclient import TestClient

from aakaar.api import AppDependencies, create_app
from aakaar.api.auth import hash_password
from aakaar.api.repositories import agents as agents_repo
from aakaar.core.config import load_settings
from aakaar.db.models import RemoteAgent, RemoteAgentStatus
from aakaar.workers.remote.broker_link import (
    BrokerLink,
    authenticate_agent_key,
    master_link_url,
    parse_agent_key,
)
from aakaar.workers.remote.protocol import RemoteTask
from tests._api_helpers import seed_tenant_admin

TOKEN = "test-broker-token"


class StubBroker:
    """Loopback websockets server standing in for aakaar-broker: every accepted
    master-link connection is handed to the test, which scripts the envelopes."""

    def __init__(self) -> None:
        self._server = None
        self.links: asyncio.Queue = asyncio.Queue()

    async def __aenter__(self) -> StubBroker:
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"ws://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"

    async def _handler(self, ws) -> None:
        await self.links.put(ws)
        await ws.wait_closed()  # the test drives send/recv; just hold it open

    async def accept(self, timeout: float = 5.0):
        return await asyncio.wait_for(self.links.get(), timeout)


class _Recorder:
    """Collects relayed agent events without touching the runs tables."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, **kw) -> None:
        self.events.append(kw)


@pytest.fixture()
def enrolled(deps: AppDependencies) -> tuple[uuid.UUID, uuid.UUID, str]:
    """A tenant with one enrolled agent; returns (tenant_id, agent_id, key)."""
    tenant, admin = seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="admin@a.test", admin_password="adminpass1"
    )
    secret = "agent-s3cret"
    with deps.session_factory.session() as s:
        agent = agents_repo.create_enrollment(
            s,
            tenant_id=tenant.id,
            alias="lab-1",
            api_key_hash=hash_password(secret),
            created_by=admin.id,
            pools=["branch"],
        )
        s.commit()
        agent_id = agent.id
    return tenant.id, agent_id, f"{agent_id}.{secret}"


def _link(deps: AppDependencies, url: str, **kw) -> BrokerLink:
    kw.setdefault("recorder", None)
    return BrokerLink(
        url=url,
        token=TOKEN,
        session_factory=deps.session_factory,
        agent_registry=deps.agent_registry,
        reconnect_delay=0.05,
        **kw,
    )


def _hello() -> str:
    return json.dumps(
        {
            "type": "hello",
            "os": "linux",
            "gui": False,
            "version": "1",
            "capabilities": [{"ref": "x.op", "version": "1"}],
        }
    )


async def _send(ws, **envelope) -> None:
    await ws.send(json.dumps(envelope))


async def _recv(ws, timeout: float = 5.0) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def _wait_for(predicate: Callable[[], object], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


# ---------- helpers / key verification ---------------------------------------


def test_master_link_url_normalization() -> None:
    assert master_link_url("ws://h:9300") == "ws://h:9300/ws/master"
    assert master_link_url("wss://h/") == "wss://h/ws/master"
    assert master_link_url("http://h:9300") == "ws://h:9300/ws/master"
    assert master_link_url(" https://broker.example.com ") == "wss://broker.example.com/ws/master"


def test_parse_agent_key_shapes() -> None:
    aid = uuid.uuid4()
    assert parse_agent_key(f"{aid}.s3cret") == (aid, "s3cret")
    assert parse_agent_key(None) is None
    assert parse_agent_key("") is None
    assert parse_agent_key("no-dot") is None
    assert parse_agent_key("not-a-uuid.x") is None


def test_authenticate_agent_key_matches_direct_path(
    deps: AppDependencies, enrolled: tuple[uuid.UUID, uuid.UUID, str]
) -> None:
    tenant_id, agent_id, key = enrolled
    ident = authenticate_agent_key(deps.session_factory, key)
    assert ident is not None
    assert (ident.agent_id, ident.tenant_id, ident.alias, ident.pools) == (
        agent_id,
        tenant_id,
        "lab-1",
        ("branch",),
    )
    assert authenticate_agent_key(deps.session_factory, f"{agent_id}.wrong") is None
    assert authenticate_agent_key(deps.session_factory, f"{uuid.uuid4()}.x") is None
    assert authenticate_agent_key(deps.session_factory, None) is None


# ---------- relayed sessions over a stub broker -------------------------------


@pytest.mark.asyncio
async def test_relayed_agent_registers_dispatches_and_unregisters(
    deps: AppDependencies, enrolled: tuple[uuid.UUID, uuid.UUID, str]
) -> None:
    tenant_id, agent_id, key = enrolled
    recorder = _Recorder()
    async with StubBroker() as broker:
        link = _link(deps, broker.url, recorder=recorder)
        await link.start()
        try:
            ws = await broker.accept()
            assert ws.request.path == "/ws/master"
            assert ws.request.headers["X-Broker-Token"] == TOKEN

            await _send(ws, t="open", sid="s1", headers={"x-agent-key": key})
            await _send(ws, t="data", sid="s1", frame=_hello())

            out = await _recv(ws)
            assert (out["t"], out["sid"]) == ("data", "s1")
            assert json.loads(out["frame"]) == {"type": "welcome", "alias": "lab-1"}

            conn = deps.agent_registry.get(tenant_id, "lab-1")
            assert conn is not None
            assert conn.info.os == "linux"
            # Pools come from the enrollment row, never from the agent's hello.
            assert conn.info.pools == ("branch",)
            with deps.session_factory.session() as s:
                row = s.get(RemoteAgent, agent_id)
                assert row is not None and row.status == RemoteAgentStatus.ONLINE

            # Task dispatch round-trips through the envelope framing.
            task = RemoteTask(task_id="t1", run_id="", node_id="", ref="x.op", inputs={"a": 1})
            pending = asyncio.ensure_future(conn.dispatch(task))
            env = await _recv(ws)
            wire = json.loads(env["frame"])
            assert env["sid"] == "s1"
            assert (wire["type"], wire["ref"], wire["inputs"]) == ("task", "x.op", {"a": 1})
            await _send(
                ws,
                t="data",
                sid="s1",
                frame=json.dumps(
                    {"type": "result", "task_id": "t1", "ok": True, "outputs": {"echo": 1}}
                ),
            )
            result = await asyncio.wait_for(pending, 5)
            assert result.ok and result.outputs == {"echo": 1}

            # Event frames reach the recorder, stamped with the verified tenant.
            run_id = uuid.uuid4()
            await _send(
                ws,
                t="data",
                sid="s1",
                frame=json.dumps(
                    {
                        "type": "event",
                        "run_id": str(run_id),
                        "node_id": "n",
                        "kind": "log",
                        "payload": {"m": 1},
                    }
                ),
            )
            await _wait_for(lambda: recorder.events)
            assert recorder.events[0]["run_id"] == run_id
            assert recorder.events[0]["tenant_id"] == tenant_id

            # Broker reports the agent gone -> unregistered + marked offline.
            await _send(ws, t="close", sid="s1")
            await _wait_for(lambda: deps.agent_registry.get(tenant_id, "lab-1") is None)
            with deps.session_factory.session() as s:
                row = s.get(RemoteAgent, agent_id)
                assert row is not None and row.status == RemoteAgentStatus.OFFLINE
        finally:
            await link.stop()


@pytest.mark.asyncio
async def test_bad_or_missing_key_is_closed_and_never_registered(
    deps: AppDependencies, enrolled: tuple[uuid.UUID, uuid.UUID, str]
) -> None:
    tenant_id, agent_id, _ = enrolled
    async with StubBroker() as broker:
        link = _link(deps, broker.url)
        await link.start()
        try:
            ws = await broker.accept()
            await _send(ws, t="open", sid="s1", headers={"x-agent-key": f"{agent_id}.wrong"})
            assert await _recv(ws) == {"t": "close", "sid": "s1"}
            await _send(ws, t="open", sid="s2", headers={})
            assert await _recv(ws) == {"t": "close", "sid": "s2"}
            assert deps.agent_registry.get(tenant_id, "lab-1") is None
        finally:
            await link.stop()


@pytest.mark.asyncio
async def test_session_without_hello_is_dropped(
    deps: AppDependencies, enrolled: tuple[uuid.UUID, uuid.UUID, str]
) -> None:
    _, _, key = enrolled
    async with StubBroker() as broker:
        link = _link(deps, broker.url, hello_timeout=0.2)
        await link.start()
        try:
            ws = await broker.accept()
            await _send(ws, t="open", sid="s1", headers={"x-agent-key": key})
            # Key verifies, but no hello ever arrives -> the link gives up.
            assert await _recv(ws) == {"t": "close", "sid": "s1"}
        finally:
            await link.stop()


@pytest.mark.asyncio
async def test_link_redials_after_drop(deps: AppDependencies) -> None:
    async with StubBroker() as broker:
        link = _link(deps, broker.url)
        await link.start()
        try:
            first = await broker.accept()
            await first.close()
            second = await broker.accept()  # reconnected (with backoff + jitter)
            assert second.request.headers["X-Broker-Token"] == TOKEN
        finally:
            await link.stop()


@pytest.mark.asyncio
async def test_master_link_loss_unwinds_relayed_sessions(
    deps: AppDependencies, enrolled: tuple[uuid.UUID, uuid.UUID, str]
) -> None:
    tenant_id, _, key = enrolled
    async with StubBroker() as broker:
        link = _link(deps, broker.url)
        await link.start()
        try:
            ws = await broker.accept()
            await _send(ws, t="open", sid="s1", headers={"x-agent-key": key})
            await _send(ws, t="data", sid="s1", frame=_hello())
            await _recv(ws)  # welcome: registration is complete
            assert deps.agent_registry.get(tenant_id, "lab-1") is not None
            await ws.close()
            await _wait_for(lambda: deps.agent_registry.get(tenant_id, "lab-1") is None)
        finally:
            await link.stop()


# ---------- app + settings wiring ---------------------------------------------


def test_create_app_fails_closed_on_url_without_token(deps: AppDependencies) -> None:
    deps.settings.broker_url = "ws://127.0.0.1:9"
    deps.settings.broker_token = None
    with pytest.raises(RuntimeError, match="broker_token"):
        create_app(deps)


def test_no_broker_configured_means_no_link(deps: AppDependencies) -> None:
    app = create_app(deps)
    assert app.state.broker_link is None


def test_broker_link_not_started_when_remote_exec_disabled(deps: AppDependencies) -> None:
    deps.settings.broker_url = "ws://127.0.0.1:9"
    deps.settings.broker_token = TOKEN
    deps.settings.remote_exec_enabled = False
    app = create_app(deps)
    assert app.state.broker_link is None  # parity with /ws/agents refusing connects


def test_lifespan_dials_the_broker(deps: AppDependencies) -> None:
    """With AAKAAR_BROKER_URL configured, app startup establishes the master
    link (and shutdown tears it down without hanging)."""
    seen_tokens: queue.Queue[str | None] = queue.Queue()
    port_box: queue.Queue[int] = queue.Queue()
    loop_box: queue.Queue = queue.Queue()

    def serve() -> None:  # the stub broker needs its own loop: TestClient owns ours
        async def main() -> None:
            stop = asyncio.Event()
            loop_box.put((asyncio.get_running_loop(), stop))

            async def handler(ws) -> None:
                seen_tokens.put(ws.request.headers.get("X-Broker-Token"))
                await ws.wait_closed()

            server = await websockets.serve(handler, "127.0.0.1", 0)
            port_box.put(server.sockets[0].getsockname()[1])
            await stop.wait()
            server.close()
            await server.wait_closed()

        asyncio.run(main())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    loop, stop = loop_box.get(timeout=5)
    try:
        deps.settings.broker_url = f"ws://127.0.0.1:{port_box.get(timeout=5)}"
        deps.settings.broker_token = TOKEN
        app = create_app(deps)
        assert app.state.broker_link is not None
        with TestClient(app):
            assert seen_tokens.get(timeout=5) == TOKEN
        # Exiting the client ran lifespan shutdown -> the link stopped cleanly.
    finally:
        loop.call_soon_threadsafe(stop.set)
        thread.join(timeout=5)


def test_load_settings_fails_closed_on_broker_url_without_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # keep load_dotenv away from the repo .env
    monkeypatch.setenv("AAKAAR_JWT_SECRET", "x" * 48)
    monkeypatch.setenv("AAKAAR_BROKER_URL", "wss://broker.example.com")
    monkeypatch.delenv("AAKAAR_BROKER_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="AAKAAR_BROKER_TOKEN"):
        load_settings()

    monkeypatch.setenv("AAKAAR_BROKER_TOKEN", "tok")
    s = load_settings()
    assert (s.broker_url, s.broker_token) == ("wss://broker.example.com", "tok")

    monkeypatch.delenv("AAKAAR_BROKER_URL")
    s = load_settings()
    assert (s.broker_url, s.broker_token) == (None, "tok")  # token alone is inert
