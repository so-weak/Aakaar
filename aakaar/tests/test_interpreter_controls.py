"""ControlHub / RunControlHandle semantics — pause gate + cooperative cancel."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from aakaar.interpreter.controls import ControlHub, RunCancelled, RunNotActive


async def test_checkpoint_passes_when_gate_open() -> None:
    hub = ControlHub()
    handle = hub.register(uuid.uuid4(), uuid.uuid4())
    await handle.checkpoint()  # gate starts open — must not block or raise
    assert not handle.paused
    assert not handle.cancel_requested


async def test_pause_blocks_checkpoint_until_resume() -> None:
    hub = ControlHub()
    handle = hub.register(uuid.uuid4(), uuid.uuid4())
    handle.pause()
    assert handle.paused

    waiter = asyncio.create_task(handle.checkpoint())
    await asyncio.sleep(0.05)
    assert not waiter.done(), "checkpoint must block while paused"

    handle.resume()
    await asyncio.wait_for(waiter, timeout=1)
    assert not handle.paused


async def test_cancel_raises_at_checkpoint() -> None:
    hub = ControlHub()
    handle = hub.register(uuid.uuid4(), uuid.uuid4())
    handle.request_cancel()
    with pytest.raises(RunCancelled):
        await handle.checkpoint()


async def test_cancel_releases_a_paused_checkpoint() -> None:
    hub = ControlHub()
    handle = hub.register(uuid.uuid4(), uuid.uuid4())
    handle.pause()
    waiter = asyncio.create_task(handle.checkpoint())
    await asyncio.sleep(0.05)
    assert not waiter.done()

    handle.request_cancel()  # must open the gate AND mark the cancel
    with pytest.raises(RunCancelled):
        await asyncio.wait_for(waiter, timeout=1)


def test_get_unknown_run_raises() -> None:
    hub = ControlHub()
    missing = uuid.uuid4()
    with pytest.raises(RunNotActive):
        hub.get(missing)
    assert hub.peek(missing) is None


def test_discard_is_idempotent() -> None:
    hub = ControlHub()
    run_id = uuid.uuid4()
    hub.register(run_id, uuid.uuid4())
    hub.discard(run_id)
    hub.discard(run_id)  # second discard is a no-op
    with pytest.raises(RunNotActive):
        hub.get(run_id)
