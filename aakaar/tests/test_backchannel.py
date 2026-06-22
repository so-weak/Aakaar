"""Stage 2 — the bidirectional back-channel multiplexer.

Exercises both fabrics over a fake socket, independent of the task/result flow:
  - agent -> server  req  / server -> agent reply  (via send_reply)
  - server -> agent  ctrl / agent  -> server ack   (via request + resolve_ack)
and the shared demux router that both server read loops use.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from aakaar.workers.remote.backchannel import demux_agent_frame
from aakaar.workers.remote.connection import WebSocketAgentConnection
from aakaar.workers.remote.protocol import AgentInfo


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, frame: dict) -> None:
        self.sent.append(frame)

    async def close(self) -> None:  # pragma: no cover - trivial
        pass


def _conn() -> tuple[WebSocketAgentConnection, _FakeWS]:
    ws = _FakeWS()
    info = AgentInfo(alias="a1", tenant_id=uuid.uuid4())
    return WebSocketAgentConnection(ws, info), ws


async def test_send_reply_frame_shape() -> None:
    conn, ws = _conn()
    await conn.send_reply("r1", ok=True, result={"x": 1})
    assert ws.sent == [{"type": "reply", "request_id": "r1", "ok": True, "result": {"x": 1}, "error": None}]


async def test_request_resolves_on_ack() -> None:
    conn, ws = _conn()
    task = asyncio.create_task(conn.request("run_end", {"run_id": "R"}))
    await asyncio.sleep(0)  # let request() send the ctrl frame
    frame = ws.sent[-1]
    assert frame["type"] == "ctrl" and frame["op"] == "run_end" and frame["run_id"] == "R"
    # The demux routes the agent's ack back to the waiting future.
    await demux_agent_frame(
        conn, {"type": "ack", "request_id": frame["request_id"], "ok": True}, on_event=lambda _m: None
    )
    ack = await asyncio.wait_for(task, timeout=1)
    assert ack["ok"] is True


async def test_request_fails_when_connection_drops() -> None:
    conn, _ws = _conn()
    task = asyncio.create_task(conn.request("cancel", {"run_id": "R"}))
    await asyncio.sleep(0)
    conn.fail_pending("agent disconnected")
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(task, timeout=1)


async def test_demux_req_without_handler_replies_disabled() -> None:
    conn, ws = _conn()
    await demux_agent_frame(
        conn, {"type": "req", "request_id": "q1", "op": "obj_get"}, on_event=lambda _m: None
    )
    reply = ws.sent[-1]
    assert reply["type"] == "reply" and reply["request_id"] == "q1" and reply["ok"] is False
    assert reply["error"]["type"] == "Disabled"


async def test_demux_req_with_handler_is_invoked() -> None:
    conn, _ws = _conn()
    seen: list[dict] = []

    async def handler(c: WebSocketAgentConnection, msg: dict) -> None:
        seen.append(msg)
        await c.send_reply(msg["request_id"], ok=True, result={"pong": True})

    await demux_agent_frame(
        conn, {"type": "req", "request_id": "q2", "op": "obj_put"}, on_event=lambda _m: None, request_handler=handler
    )
    assert seen and seen[0]["op"] == "obj_put"


async def test_demux_event_sink() -> None:
    conn, _ws = _conn()
    events: list[dict] = []
    await demux_agent_frame(
        conn, {"type": "event", "run_id": "R", "kind": "log"}, on_event=events.append
    )
    assert events and events[0]["kind"] == "log"
