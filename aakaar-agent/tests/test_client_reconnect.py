"""AgentClient reconnect behavior: backoff/jitter, result re-delivery after a
mid-task disconnect, and task_id dedup (no duplicate execution)."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from aakaar_agent import capabilities
from aakaar_agent.client import _BACKOFF_CAP, AgentClient


# -- backoff -------------------------------------------------------------------


def test_backoff_grows_exponentially_with_jitter_and_cap() -> None:
    client = AgentClient("ws://unused", "k", reconnect_delay=1.0)
    delays = [client._next_delay() for _ in range(12)]
    for attempt, delay in enumerate(delays):
        ceiling = min(_BACKOFF_CAP, 2**attempt)
        assert ceiling * 0.5 <= delay <= ceiling
    assert delays[-1] <= _BACKOFF_CAP
    assert delays[-1] >= _BACKOFF_CAP * 0.5  # capped, not collapsed to zero


def test_backoff_resets_after_attempt_counter_cleared() -> None:
    client = AgentClient("ws://unused", "k", reconnect_delay=1.0)
    for _ in range(6):
        client._next_delay()
    client._attempts = 0  # what run_forever does after a stable connection
    assert client._next_delay() <= 1.0


# -- in-flight result handling ---------------------------------------------------


class _SlowCap:
    """Capability that records how many times it ran and waits on an event so
    the test controls when the result becomes available."""

    REF = "cap.test_slow"
    VERSION = "1"
    GUI = False

    def __init__(self) -> None:
        self.runs = 0
        self.release = asyncio.Event()

    async def run(self, inputs: dict, secrets: dict) -> dict:
        self.runs += 1
        await self.release.wait()
        return {"ran": self.runs}


@pytest.fixture
def slow_cap():
    cap = _SlowCap()
    capabilities._REGISTRY[cap.REF] = cap
    yield cap
    capabilities._REGISTRY.pop(cap.REF, None)


async def test_result_redelivered_after_reconnect_without_reexecution(slow_cap) -> None:
    connections = 0
    results: list[dict] = []
    got_result = asyncio.Event()

    async def handler(ws) -> None:
        nonlocal connections
        connections += 1
        await ws.recv()  # hello
        if connections == 1:
            await ws.send(
                json.dumps({"type": "task", "task_id": "t1", "ref": "cap.test_slow"})
            )
            await asyncio.sleep(0.05)  # let the agent start the task
            await ws.close()  # drop the link while t1 is in flight
            slow_cap.release.set()  # task finishes with no connection up
        else:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "result":
                    results.append(msg)
                    got_result.set()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = AgentClient(f"ws://127.0.0.1:{port}", "k", reconnect_delay=0.05)
        runner = asyncio.create_task(client.run_forever())
        await asyncio.wait_for(got_result.wait(), timeout=10)
        client.stop()
        await asyncio.wait_for(runner, timeout=10)

    assert connections >= 2
    assert slow_cap.runs == 1  # never executed twice
    assert results[0] == {
        "type": "result",
        "task_id": "t1",
        "ok": True,
        "outputs": {"ran": 1},
    }


async def test_redispatch_of_known_task_id_served_from_cache(slow_cap) -> None:
    results: list[dict] = []
    two_results = asyncio.Event()

    async def handler(ws) -> None:
        await ws.recv()  # hello
        slow_cap.release.set()
        task = json.dumps({"type": "task", "task_id": "t2", "ref": "cap.test_slow"})
        await ws.send(task)
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "result":
                results.append(msg)
                if len(results) == 1:
                    await ws.send(task)  # server retries the same task_id
                else:
                    two_results.set()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = AgentClient(f"ws://127.0.0.1:{port}", "k", reconnect_delay=0.05)
        runner = asyncio.create_task(client.run_forever())
        await asyncio.wait_for(two_results.wait(), timeout=10)
        client.stop()
        await asyncio.wait_for(runner, timeout=10)

    assert slow_cap.runs == 1  # second dispatch answered from the result cache
    assert results[0] == results[1]


async def test_duplicate_dispatch_while_inflight_runs_once(slow_cap) -> None:
    client = AgentClient("ws://unused", "k")
    msg = {"type": "task", "task_id": "t3", "ref": "cap.test_slow"}
    client._handle_task(msg)
    client._handle_task(msg)  # duplicate while the first is still running
    await asyncio.sleep(0)  # let the task start
    assert len(client._inflight) == 1
    slow_cap.release.set()
    await asyncio.wait_for(client._inflight["t3"], timeout=5)
    assert slow_cap.runs == 1
    # no connection: the reply must be buffered for re-delivery, not lost
    assert "t3" in client._undelivered
    assert client._results["t3"]["ok"] is True


async def test_result_cache_is_bounded() -> None:
    from aakaar_agent import client as client_mod

    client = AgentClient("ws://unused", "k")
    for i in range(client_mod._RESULT_CACHE_MAX + 10):
        task_id = f"t{i}"
        client._results[task_id] = {"task_id": task_id}
        client._undelivered.add(task_id)
    # trimming happens in _run_and_reply; exercise it via one real task
    capabilities._REGISTRY["cap.test_noop"] = type(
        "Noop", (), {"REF": "cap.test_noop", "run": staticmethod(lambda i, s: {})}
    )
    try:
        await client._run_and_reply({"task_id": "last", "ref": "cap.test_noop"})
    finally:
        capabilities._REGISTRY.pop("cap.test_noop", None)
    assert len(client._results) <= client_mod._RESULT_CACHE_MAX
    assert len(client._undelivered) <= len(client._results)
