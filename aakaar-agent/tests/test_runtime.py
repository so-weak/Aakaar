"""Stage 3 — the agent browser/capability runtime.

Uses FakeBrowserPool (no real Chromium) to exercise the parts that make the
SAME shared browser caps run on the agent: per-run session_state shared across
nodes, open_session idempotency, run_end teardown, and the result-cache
exclusion rules.
"""

from __future__ import annotations

import asyncio

import pytest

from aakaar_agent.capabilities import dispatch, load_capabilities
from aakaar_agent.runtime import AgentRuntime, is_uncacheable
from aakaar_caps.browser.fake import FakeBrowserPool


class _FakeClient:
    """Stands in for AgentClient: provides a loop and a send_request stub."""

    def __init__(self) -> None:
        self._loop = asyncio.get_event_loop()
        self.requests: list[tuple[str, dict]] = []

    async def send_request(self, op: str, **payload):
        self.requests.append((op, payload))
        if op == "obj_put":
            return {"uri": "aakaar://t/x/k"}
        if op == "obj_get":
            return {"b64": ""}
        return {}


def _runtime() -> AgentRuntime:
    load_capabilities()
    return AgentRuntime(_FakeClient(), pool_factory=FakeBrowserPool)


async def test_session_shared_across_nodes_in_a_run() -> None:
    rt = _runtime()
    # Node 1 opens a session.
    ctx1 = rt.build_context(secrets={}, run_id="R1", node_id="open", tenant_id="t1")
    out = await dispatch("browser.open_session", {}, {}, context=ctx1)
    sid = out["session"]
    assert sid
    # Node 2 (same run) navigates the SAME session — proves shared session_state.
    ctx2 = rt.build_context(secrets={}, run_id="R1", node_id="nav", tenant_id="t1")
    await dispatch("browser.navigate", {"session": sid, "url": "https://x"}, {}, context=ctx2)
    # A different run cannot see it.
    ctx_other = rt.build_context(secrets={}, run_id="R2", node_id="nav", tenant_id="t1")
    with pytest.raises(RuntimeError):
        await dispatch("browser.navigate", {"session": sid, "url": "https://x"}, {}, context=ctx_other)


async def test_open_session_idempotent_per_node() -> None:
    rt = _runtime()
    ctx = rt.build_context(secrets={}, run_id="R1", node_id="open", tenant_id="t1")
    out1 = await dispatch("browser.open_session", {}, {}, context=ctx)
    rt.record_open("t1", "R1", "open", out1)
    # A retry of the same node returns the cached session, not a new one.
    cached = rt.cached_open("t1", "R1", "open")
    assert cached == out1
    assert rt.cached_open("t1", "R1", "other") is None


async def test_run_end_closes_sessions() -> None:
    rt = _runtime()
    ctx = rt.build_context(secrets={}, run_id="R1", node_id="open", tenant_id="t1")
    out = await dispatch("browser.open_session", {}, {}, context=ctx)
    sid = out["session"]
    await rt.end_run("t1", "R1")
    # State is gone; a navigate against the old id now fails (session reaped).
    ctx2 = rt.build_context(secrets={}, run_id="R1", node_id="nav", tenant_id="t1")
    with pytest.raises(RuntimeError):
        await dispatch("browser.navigate", {"session": sid, "url": "https://x"}, {}, context=ctx2)


async def test_screenshot_uses_object_writer_proxy() -> None:
    rt = _runtime()
    client = rt._client
    ctx = rt.build_context(secrets={}, run_id="R1", node_id="open", tenant_id="t1")
    out = await dispatch("browser.open_session", {}, {}, context=ctx)
    sid = out["session"]
    ctx2 = rt.build_context(secrets={}, run_id="R1", node_id="shot", tenant_id="t1")
    res = await dispatch("browser.screenshot", {"session": sid}, {}, context=ctx2)
    assert res["image_uri"] == "aakaar://t/x/k"
    assert any(op == "obj_put" for op, _ in client.requests)


def test_cache_exclusion_rules() -> None:
    assert is_uncacheable("browser.open_session")
    assert is_uncacheable("browser.navigate")
    assert is_uncacheable("cap.screenshot")
    assert is_uncacheable("cap.open_url")
    assert is_uncacheable("cap.file_download")
    assert is_uncacheable("cap.web_login")
    assert not is_uncacheable("cap.json_extract")
    assert not is_uncacheable("cap.shell_exec")
    assert not is_uncacheable("cap.desktop_click")
