"""Relay behavior over real websockets on a loopback port: token enforcement,
pairing + blind frame relay, handshake timeout, the session cap, and master
replacement."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from aakaar_broker.relay import BrokerSettings, RendezvousBroker, load_broker_settings

TOKEN = "test-broker-token"


async def _start(**overrides) -> RendezvousBroker:
    settings = BrokerSettings(token=TOKEN, host="127.0.0.1", port=0, **overrides)
    broker = RendezvousBroker(settings)
    await broker.start()
    return broker


def _master_url(broker: RendezvousBroker) -> str:
    return f"ws://127.0.0.1:{broker.port}/ws/master"


def _agent_url(broker: RendezvousBroker) -> str:
    return f"ws://127.0.0.1:{broker.port}/ws/agents"


async def _connect_master(broker: RendezvousBroker, token: str = TOKEN):
    return await websockets.connect(
        _master_url(broker), additional_headers={"X-Broker-Token": token}
    )


async def _connect_agent(broker: RendezvousBroker, key: str = "aid.secret"):
    return await websockets.connect(
        _agent_url(broker), additional_headers={"X-Agent-Key": key}
    )


async def _recv_json(ws, timeout: float = 5.0) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def _wait_closed(ws, timeout: float = 5.0) -> int | None:
    await asyncio.wait_for(ws.wait_closed(), timeout)
    return ws.close_code


# ---------- configuration ----------------------------------------------------


def test_settings_refuse_missing_token() -> None:
    with pytest.raises(RuntimeError, match="AAKAAR_BROKER_TOKEN"):
        load_broker_settings(env={})
    with pytest.raises(RuntimeError, match="AAKAAR_BROKER_TOKEN"):
        load_broker_settings(env={"AAKAAR_BROKER_TOKEN": "   "})


def test_settings_read_from_env() -> None:
    s = load_broker_settings(
        env={
            "AAKAAR_BROKER_TOKEN": "t",
            "AAKAAR_BROKER_HOST": "0.0.0.0",
            "AAKAAR_BROKER_PORT": "9999",
            "AAKAAR_BROKER_MAX_SESSIONS": "7",
            "AAKAAR_BROKER_HANDSHAKE_TIMEOUT": "2.5",
        }
    )
    assert (s.token, s.host, s.port, s.max_sessions, s.handshake_timeout) == (
        "t",
        "0.0.0.0",
        9999,
        7,
        2.5,
    )


def test_settings_defaults() -> None:
    s = load_broker_settings(env={"AAKAAR_BROKER_TOKEN": "t"})
    assert (s.host, s.port, s.max_sessions, s.handshake_timeout) == ("127.0.0.1", 9300, 200, 10.0)


def test_broker_refuses_empty_token() -> None:
    with pytest.raises(ValueError, match="token"):
        RendezvousBroker(BrokerSettings(token=""))


# ---------- master-link auth ---------------------------------------------------


async def test_master_with_bad_token_is_rejected() -> None:
    broker = await _start()
    try:
        ws = await _connect_master(broker, token="wrong")
        assert await _wait_closed(ws) == 4401
        ws = await websockets.connect(_master_url(broker))  # no token at all
        assert await _wait_closed(ws) == 4401
    finally:
        await broker.stop()


async def test_unknown_path_is_rejected() -> None:
    broker = await _start()
    try:
        ws = await websockets.connect(f"ws://127.0.0.1:{broker.port}/ws/other")
        assert await _wait_closed(ws) == 1008
    finally:
        await broker.stop()


# ---------- pairing + relay ----------------------------------------------------


async def test_agent_without_master_is_turned_away() -> None:
    broker = await _start()
    try:
        ws = await _connect_agent(broker)
        assert await _wait_closed(ws) == 1013
    finally:
        await broker.stop()


async def test_pairs_and_relays_frames_both_ways() -> None:
    broker = await _start()
    try:
        master = await _connect_master(broker)
        agent = await _connect_agent(broker, key="aid.s3cret")

        opened = await _recv_json(master)
        assert opened["t"] == "open"
        sid = opened["sid"]
        # The agent key transits the broker in cleartext inside the open
        # envelope (the API does the authoritative DB check). This is exactly
        # why the broker host is trusted infrastructure — see the README trust
        # model; this assertion is the regression anchor for that fact.
        assert opened["headers"] == {"x-agent-key": "aid.s3cret"}

        hello = json.dumps({"type": "hello", "os": "linux"})
        await agent.send(hello)
        assert await _recv_json(master) == {"t": "data", "sid": sid, "frame": hello}

        welcome = json.dumps({"type": "welcome", "alias": "lab-1"})
        await master.send(json.dumps({"t": "data", "sid": sid, "frame": welcome}))
        assert await asyncio.wait_for(agent.recv(), 5) == welcome  # relayed verbatim

        # Agent goes away -> master is told the session closed.
        await agent.close()
        assert await _recv_json(master) == {"t": "close", "sid": sid}
        await master.close()
    finally:
        await broker.stop()


async def test_slow_agent_does_not_stall_dispatch_to_the_fleet(monkeypatch) -> None:
    """Head-of-line regression: one agent socket that won't drain (its send
    blocks) must not stall the master link's dispatch to every OTHER agent.

    A genuinely non-draining TCP peer is unreliable to reproduce on loopback
    (the kernel buffers freely), so we simulate it at the send boundary: the
    relay's per-agent send for the stalled sid blocks forever. With the buggy
    inline `await _quiet_send(...)` in the shared master read loop, that one
    blocked send freezes dispatch to the healthy agent too."""
    import aakaar_broker.relay as relay

    broker = await _start()
    blocked_sids: set[str] = set()
    real_send = relay._quiet_send

    async def gated_send(ws, frame):  # type: ignore[no-untyped-def]
        # Identify the stalled socket by the marker frame the test sends first.
        if frame == "STALL":
            blocked_sids.add(id(ws))  # remember this socket; never deliver
            await asyncio.Event().wait()  # block this send forever
            return
        if id(ws) in blocked_sids:
            await asyncio.Event().wait()
            return
        await real_send(ws, frame)

    monkeypatch.setattr(relay, "_quiet_send", gated_send)
    try:
        master = await _connect_master(broker)
        stalled = await _connect_agent(broker, key="aid.stalled")
        healthy = await _connect_agent(broker, key="aid.healthy")

        sids = {}
        for _ in range(2):
            opened = await _recv_json(master)
            sids[opened["headers"]["x-agent-key"]] = opened["sid"]
        stalled_sid = sids["aid.stalled"]
        healthy_sid = sids["aid.healthy"]

        # Wedge the stalled agent's send path (blocks forever in gated_send).
        await master.send(json.dumps({"t": "data", "sid": stalled_sid, "frame": "STALL"}))
        await asyncio.sleep(0.1)

        # A frame for the HEALTHY agent must still get through. If dispatch is
        # serialized behind the wedged send, this recv times out.
        ping = json.dumps({"type": "task", "n": 1})
        await master.send(json.dumps({"t": "data", "sid": healthy_sid, "frame": ping}))
        assert await asyncio.wait_for(healthy.recv(), 5) == ping

        await stalled.close()
        await healthy.close()
        await master.close()
    finally:
        await broker.stop()


async def test_nondraining_agent_is_dropped_when_its_downlink_overflows(monkeypatch) -> None:
    """A non-draining agent must not buffer master->agent frames without bound:
    once its per-session downlink queue fills, that agent is dropped (1013) and
    a healthy agent keeps receiving. Anchors the bounded-backpressure half of
    the head-of-line fix (the per-session queue isolation is the other half)."""
    import aakaar_broker.relay as relay

    broker = await _start()
    blocked_sids: set[str] = set()
    real_send = relay._quiet_send

    async def gated_send(ws, frame):  # type: ignore[no-untyped-def]
        if frame == "STALL":
            blocked_sids.add(id(ws))
            await asyncio.Event().wait()  # this socket never drains again
            return
        if id(ws) in blocked_sids:
            await asyncio.Event().wait()
            return
        await real_send(ws, frame)

    monkeypatch.setattr(relay, "_quiet_send", gated_send)
    try:
        master = await _connect_master(broker)
        stalled = await _connect_agent(broker, key="aid.stalled")
        healthy = await _connect_agent(broker, key="aid.healthy")

        sids = {}
        for _ in range(2):
            opened = await _recv_json(master)
            sids[opened["headers"]["x-agent-key"]] = opened["sid"]
        stalled_sid, healthy_sid = sids["aid.stalled"], sids["aid.healthy"]

        # Wedge the stalled agent's drainer, then flood its downlink past the
        # bound so it cannot be buffered without limit.
        await master.send(json.dumps({"t": "data", "sid": stalled_sid, "frame": "STALL"}))
        for i in range(relay._DOWNLINK_QUEUE_MAX + 50):
            await master.send(json.dumps({"t": "data", "sid": stalled_sid, "frame": f"f{i}"}))

        # The flooded agent is closed; the close envelope still reaches master.
        assert await _wait_closed(stalled, timeout=5) == 1013
        assert await _recv_json(master) == {"t": "close", "sid": stalled_sid}

        # The healthy agent is untouched and still receives its frame.
        ping = json.dumps({"type": "task", "n": 1})
        await master.send(json.dumps({"t": "data", "sid": healthy_sid, "frame": ping}))
        assert await asyncio.wait_for(healthy.recv(), 5) == ping

        await healthy.close()
        await master.close()
    finally:
        await broker.stop()


async def test_master_can_inject_arbitrary_frames_on_an_open_session() -> None:
    # Threat-model regression: once a session is open, the broker (master link)
    # is the SOLE source of frames the peer receives for that sid, so a hostile
    # broker/master can forge any frame. The relay must pass it through blindly
    # — that blindness is precisely why the broker host is trusted (README
    # trust model). Here the master injects a frame the agent never asked for.
    broker = await _start()
    try:
        master = await _connect_master(broker)
        agent = await _connect_agent(broker)
        sid = (await _recv_json(master))["sid"]

        forged = json.dumps({"type": "task", "forged": True})
        await master.send(json.dumps({"t": "data", "sid": sid, "frame": forged}))
        assert await asyncio.wait_for(agent.recv(), 5) == forged

        await agent.close()
        await master.close()
    finally:
        await broker.stop()


async def test_master_close_frame_drops_the_agent() -> None:
    broker = await _start()
    try:
        master = await _connect_master(broker)
        agent = await _connect_agent(broker)
        sid = (await _recv_json(master))["sid"]
        await master.send(json.dumps({"t": "close", "sid": sid}))
        assert await _wait_closed(agent) == 1000
        await master.close()
    finally:
        await broker.stop()


async def test_master_loss_closes_agents() -> None:
    broker = await _start()
    try:
        master = await _connect_master(broker)
        agent = await _connect_agent(broker)
        sid = (await _recv_json(master))["sid"]
        await master.send(json.dumps({"t": "data", "sid": sid, "frame": "hi"}))
        await asyncio.wait_for(agent.recv(), 5)
        await master.close()
        assert await _wait_closed(agent) == 1012  # service restart: reconnect
        assert broker.session_count == 0
    finally:
        await broker.stop()


async def test_oversized_envelope_drops_only_the_offending_agent() -> None:
    """JSON escaping can inflate an agent frame past the master link's receive
    cap; the broker must drop that agent (1009), not poison the shared link."""
    broker = await _start()
    try:
        master = await _connect_master(broker)
        agent = await _connect_agent(broker)
        sid = (await _recv_json(master))["sid"]

        # 8 MiB of control chars escapes to a ~48 MiB envelope (> the cap).
        await agent.send("\x01" * (8 * 1024 * 1024))
        assert await _wait_closed(agent, timeout=30) == 1009
        assert await _recv_json(master, timeout=30) == {"t": "close", "sid": sid}

        # The master link survived and still serves fresh sessions.
        replacement = await _connect_agent(broker)
        assert (await _recv_json(master))["t"] == "open"
        await replacement.close()
        await master.close()
    finally:
        await broker.stop()


async def test_binary_frames_close_the_agent_session() -> None:
    broker = await _start()
    try:
        master = await _connect_master(broker)
        agent = await _connect_agent(broker)
        await _recv_json(master)  # open
        await agent.send(b"\x00binary")
        assert await _wait_closed(agent) == 1003
        await master.close()
    finally:
        await broker.stop()


# ---------- handshake timeout ----------------------------------------------------


async def test_unpaired_session_dropped_after_handshake_timeout() -> None:
    broker = await _start(handshake_timeout=0.2)
    try:
        master = await _connect_master(broker)
        agent = await _connect_agent(broker)
        await _recv_json(master)  # open announced, but the master never answers
        assert await _wait_closed(agent) == 4408
        await master.close()
    finally:
        await broker.stop()


async def test_paired_session_survives_handshake_window() -> None:
    broker = await _start(handshake_timeout=0.2)
    try:
        master = await _connect_master(broker)
        agent = await _connect_agent(broker)
        sid = (await _recv_json(master))["sid"]
        await master.send(json.dumps({"t": "data", "sid": sid, "frame": "welcome"}))
        await asyncio.wait_for(agent.recv(), 5)
        await asyncio.sleep(0.4)  # well past the handshake window
        assert agent.close_code is None  # still open
        await agent.close()
        await master.close()
    finally:
        await broker.stop()


# ---------- session cap ----------------------------------------------------------


async def test_session_cap_enforced_and_slot_freed_on_disconnect() -> None:
    broker = await _start(max_sessions=1)
    try:
        master = await _connect_master(broker)
        first = await _connect_agent(broker)
        sid = (await _recv_json(master))["sid"]
        await master.send(json.dumps({"t": "data", "sid": sid, "frame": "w"}))

        second = await _connect_agent(broker)
        assert await _wait_closed(second) == 1013  # full

        await first.close()
        assert await _recv_json(master) == {"t": "close", "sid": sid}
        third = await _connect_agent(broker)
        assert (await _recv_json(master))["t"] == "open"  # slot freed
        await third.close()
        await master.close()
    finally:
        await broker.stop()


# ---------- master replacement -----------------------------------------------------


async def test_valid_second_master_replaces_the_first() -> None:
    broker = await _start()
    try:
        old = await _connect_master(broker)
        agent = await _connect_agent(broker)
        await _recv_json(old)  # open

        new = await _connect_master(broker)
        assert await _wait_closed(old) == 1012
        assert await _wait_closed(agent) == 1012  # sessions re-pair via the new link

        # The new link serves fresh sessions.
        fresh = await _connect_agent(broker)
        assert (await _recv_json(new))["t"] == "open"
        await fresh.close()
        await new.close()
    finally:
        await broker.stop()


async def test_replacement_still_requires_a_valid_token() -> None:
    broker = await _start()
    try:
        master = await _connect_master(broker)
        intruder = await _connect_master(broker, token="wrong")
        assert await _wait_closed(intruder) == 4401
        # Original link unaffected: an agent still pairs through it.
        agent = await _connect_agent(broker)
        assert (await _recv_json(master))["t"] == "open"
        await agent.close()
        await master.close()
    finally:
        await broker.stop()
