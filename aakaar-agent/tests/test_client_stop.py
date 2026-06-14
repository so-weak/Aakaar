"""AgentClient graceful-stop semantics: stop() must tear down a live or
in-flight connection so run_forever() returns promptly, whether it is called
from inside the agent's event loop or from another thread (signal/service
handler)."""

from __future__ import annotations

import asyncio
import threading

import websockets

from aakaar_agent import client as client_mod
from aakaar_agent.client import AgentClient


async def _idle_handler(ws) -> None:
    """Accept the agent, read its hello, then sit on the socket forever — the
    server never drops it, so only the agent's own stop() can end run_forever."""
    try:
        async for _ in ws:
            pass
    except websockets.ConnectionClosed:
        pass


async def test_stop_closes_live_socket_from_within_loop() -> None:
    async with websockets.serve(_idle_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = AgentClient(f"ws://127.0.0.1:{port}", "k", reconnect_delay=0.05)
        runner = asyncio.create_task(client.run_forever())
        # Wait until the agent is actually connected (its socket is live).
        async with asyncio.timeout(10):
            while client._ws is None:
                await asyncio.sleep(0.01)
        client.stop()  # called on the running loop
        # Without a live-socket close this would hang on the idle server.
        await asyncio.wait_for(runner, timeout=10)


async def test_stop_from_another_thread_closes_live_socket() -> None:
    async with websockets.serve(_idle_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = AgentClient(f"ws://127.0.0.1:{port}", "k", reconnect_delay=0.05)
        runner = asyncio.create_task(client.run_forever())
        async with asyncio.timeout(10):
            while client._ws is None:
                await asyncio.sleep(0.01)
        # Stop from a plain thread with no running event loop — the old code's
        # `except RuntimeError: return` silently no-oped here, leaving the agent
        # parked in the read loop until the server dropped the socket.
        threading.Thread(target=client.stop).start()
        await asyncio.wait_for(runner, timeout=10)


async def test_stop_during_in_flight_dial_aborts_before_read_loop(monkeypatch) -> None:
    """stop() arriving while websockets.connect() is still dialing (so _ws is
    None) must still abort: the read loop is never entered and run_forever
    returns without waiting on the server."""
    real_connect = websockets.connect
    dialing = asyncio.Event()
    release = asyncio.Event()
    entered_read_loop = False

    class _GatedConnect:
        """Wraps websockets.connect to pause inside __aenter__, modelling a slow
        dial during which stop() is called."""

        def __init__(self, *args, **kwargs) -> None:
            self._cm = real_connect(*args, **kwargs)

        async def __aenter__(self):
            ws = await self._cm.__aenter__()
            dialing.set()
            await release.wait()
            return ws

        async def __aexit__(self, *exc):
            return await self._cm.__aexit__(*exc)

    async def _handler(ws) -> None:
        nonlocal entered_read_loop
        async for _ in ws:
            entered_read_loop = True

    monkeypatch.setattr(client_mod.websockets, "connect", _GatedConnect)

    async with websockets.serve(_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = AgentClient(f"ws://127.0.0.1:{port}", "k", reconnect_delay=0.05)
        runner = asyncio.create_task(client.run_forever())
        await asyncio.wait_for(dialing.wait(), timeout=10)
        client.stop()  # stop while the dial is still inside __aenter__
        release.set()  # let the dial land; _connect_once must now bail out
        await asyncio.wait_for(runner, timeout=10)

    assert client._ws is None
    assert not entered_read_loop  # hello/read loop were never reached


async def test_stop_before_run_forever_does_not_raise() -> None:
    # No loop captured yet (run_forever never started): stop() must be a safe
    # no-op rather than dereferencing a None loop.
    client = AgentClient("ws://unused", "k")
    client.stop()
    assert client._stop.is_set()
