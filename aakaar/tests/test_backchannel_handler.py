"""Stage 4 — the server-side back-channel request handler.

Verifies the security invariants: the tenant is taken from the authenticated
connection (never the request body), cross-tenant object reads are denied, blob
size + LLM budgets are enforced, and the LLM proxy returns the planner rationale.
"""

from __future__ import annotations

import base64
import uuid

import pytest

from aakaar.planner.llm import FakeLLMClient, PlannerCompletion
from aakaar.storage.object_store import LocalFsObjectStore
from aakaar.workers.remote.backchannel import ServerBackchannelHandler
from aakaar.workers.remote.connection import WebSocketAgentConnection
from aakaar.workers.remote.protocol import AgentInfo


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, frame: dict) -> None:
        self.sent.append(frame)

    async def close(self) -> None:  # pragma: no cover
        pass


def _conn(tenant: uuid.UUID) -> tuple[WebSocketAgentConnection, _FakeWS]:
    ws = _FakeWS()
    return WebSocketAgentConnection(ws, AgentInfo(alias="a", tenant_id=tenant)), ws


async def _run(handler, conn, frame) -> dict:
    await handler(conn, frame)
    # the handler's reply is the last frame the conn sent
    return conn._ws.sent[-1]  # type: ignore[attr-defined]


async def test_obj_put_then_get_roundtrip(tmp_path) -> None:
    store = LocalFsObjectStore(tmp_path)
    tenant = uuid.uuid4()
    handler = ServerBackchannelHandler(object_store=store)
    conn, _ws = _conn(tenant)

    payload = b"screenshot-bytes"
    put = await _run(handler, conn, {
        "type": "req", "request_id": "1", "op": "obj_put",
        "key": "runs/r/screenshots/a.png", "b64": base64.b64encode(payload).decode(),
    })
    assert put["ok"] and put["result"]["uri"].startswith(f"aakaar://t/{tenant}/")

    got = await _run(handler, conn, {
        "type": "req", "request_id": "2", "op": "obj_get", "uri": put["result"]["uri"],
    })
    assert got["ok"] and base64.b64decode(got["result"]["b64"]) == payload


async def test_obj_get_cross_tenant_denied(tmp_path) -> None:
    store = LocalFsObjectStore(tmp_path)
    owner, attacker = uuid.uuid4(), uuid.uuid4()
    handler = ServerBackchannelHandler(object_store=store)
    # owner writes a blob
    obj = store.put(str(owner), "runs/r/secret.bin", b"private")
    # attacker's connection tries to read it
    conn, _ws = _conn(attacker)
    reply = await _run(handler, conn, {"type": "req", "request_id": "1", "op": "obj_get", "uri": obj.uri})
    assert reply["ok"] is False and reply["error"]["type"] == "PermissionError"


async def test_obj_put_size_cap(tmp_path, monkeypatch) -> None:
    import aakaar.workers.remote.backchannel as bc

    monkeypatch.setattr(bc, "_MAX_OBJ_BYTES", 8)
    store = LocalFsObjectStore(tmp_path)
    handler = ServerBackchannelHandler(object_store=store)
    conn, _ws = _conn(uuid.uuid4())
    reply = await _run(handler, conn, {
        "type": "req", "request_id": "1", "op": "obj_put",
        "key": "runs/r/big.bin", "b64": base64.b64encode(b"way too many bytes").decode(),
    })
    assert reply["ok"] is False and "too large" in reply["error"]["message"]


async def test_llm_plan_returns_rationale() -> None:
    llm = FakeLLMClient(replies=[PlannerCompletion(kind="clarify", questions=["q"], rationale='{"ok":1}')])
    handler = ServerBackchannelHandler(object_store=None, llm=llm)
    conn, _ws = _conn(uuid.uuid4())
    reply = await _run(handler, conn, {
        "type": "req", "request_id": "1", "op": "llm_plan",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert reply["ok"] and reply["result"]["text"] == '{"ok":1}'


async def test_llm_budget_enforced(monkeypatch) -> None:
    import aakaar.workers.remote.backchannel as bc

    monkeypatch.setattr(bc, "_MAX_LLM_CALLS_PER_RUN", 1)
    llm = FakeLLMClient(text_replies=["a", "b"])
    handler = ServerBackchannelHandler(object_store=None, llm=llm)
    conn, _ws = _conn(uuid.uuid4())
    ok = await _run(handler, conn, {"type": "req", "request_id": "1", "op": "llm_complete", "run_id": "R", "system": "s", "user": "u"})
    assert ok["ok"]
    over = await _run(handler, conn, {"type": "req", "request_id": "2", "op": "llm_complete", "run_id": "R", "system": "s", "user": "u"})
    assert over["ok"] is False and "budget" in over["error"]["message"]
