"""The real AgentClient.send_request path (obj_put/obj_get/llm/signal proxies).

Regression for a missing `import uuid` in client.py: send_request generates a
request_id with uuid, and the runtime's object/LLM/signal proxies all go through
it — so a screenshot/download/captcha would NameError. The fake-client unit
tests don't exercise this method, so test the real one here.
"""

from __future__ import annotations

import asyncio
import json

from aakaar_agent.client import AgentClient


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


async def test_send_request_roundtrip() -> None:
    c = AgentClient("ws://x/ws/agents", "id.secret")
    c._loop = asyncio.get_running_loop()
    c._ws = _FakeWS()

    task = asyncio.create_task(c.send_request("obj_put", key="runs/r/a.png", b64="AAAA"))
    await asyncio.sleep(0)  # let send_request emit the frame
    frame = c._ws.sent[-1]
    assert frame["type"] == "req" and frame["op"] == "obj_put" and frame["key"] == "runs/r/a.png"
    assert frame["request_id"]  # uuid-generated; would NameError if uuid wasn't imported

    c._resolve_reply({"type": "reply", "request_id": frame["request_id"], "ok": True, "result": {"uri": "aakaar://t/x/k"}})
    result = await asyncio.wait_for(task, timeout=1)
    assert result == {"uri": "aakaar://t/x/k"}


async def test_send_request_error_propagates() -> None:
    c = AgentClient("ws://x/ws/agents", "id.secret")
    c._loop = asyncio.get_running_loop()
    c._ws = _FakeWS()
    task = asyncio.create_task(c.send_request("obj_get", uri="aakaar://t/x/k"))
    await asyncio.sleep(0)
    rid = c._ws.sent[-1]["request_id"]
    c._resolve_reply({"type": "reply", "request_id": rid, "ok": False, "error": {"message": "denied"}})
    try:
        await asyncio.wait_for(task, timeout=1)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "denied" in str(e)
