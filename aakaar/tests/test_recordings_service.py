"""RecordingService unit tests: bounds, TTL expiry, dispatcher wiring, and
failure cleanup — all against an in-process fake agent."""

from __future__ import annotations

import asyncio
import threading
import uuid

import pytest

from aakaar.services.recordings import (
    AgentRecordingError,
    AgentUnavailable,
    RecordingLimitReached,
    RecordingNotFound,
    RecordingService,
    RecordingUnavailable,
)
from aakaar.workers.remote import (
    AgentCapability,
    AgentInfo,
    AgentRegistry,
    FakeAgentConnection,
    RemoteDispatcher,
    RemoteResult,
)

TENANT = uuid.uuid4()
USER = uuid.uuid4()


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _scripted_handler(events: list[dict] | None = None):
    def handler(task):
        action = task.inputs["action"]
        out = {"recording_id": "agent-rec-1", "status": "recording", "event_count": 0}
        if action == "status":
            out["event_count"] = len(events or [])
        elif action == "stop":
            out = {
                "recording_id": "agent-rec-1",
                "status": "stopped",
                "event_count": len(events or []),
                "events": events or [],
            }
        elif action == "discard":
            out = {"recording_id": "agent-rec-1", "status": "discarded", "event_count": 0}
        return RemoteResult(task_id=task.task_id, ok=True, outputs=out)

    return handler


def _make_conn(handler, *, alias: str = "lab-1", pools: tuple[str, ...] = ()):
    return FakeAgentConnection(
        AgentInfo(
            alias=alias,
            tenant_id=TENANT,
            gui_capable=True,
            pools=pools,
            capabilities=(AgentCapability(ref="cap.activity_recording"),),
        ),
        handler,
    )


def _setup(handler=None, *, ttl: float = 7200.0, max_per_tenant: int = 5):
    agents = AgentRegistry()
    conn = _make_conn(handler or _scripted_handler())
    agents.register(conn)
    dispatcher = RemoteDispatcher(agents=agents, registry=None, audit=None)
    clock = _Clock()
    service = RecordingService(
        dispatcher=dispatcher,
        agents=agents,
        ttl_seconds=ttl,
        max_per_tenant=max_per_tenant,
        clock=clock,
    )
    return service, conn, clock, agents


async def _begin(service: RecordingService, name: str = "demo"):
    return await service.begin_recording(
        tenant_id=TENANT, created_by=USER, name=name, agent_alias="lab-1", max_events=2000
    )


async def _setup_and_begin(ttl: float = 10.0):
    """Build a service and start one recording fully inside a loop so any
    begin-time opportunistic drain task completes before the caller goes
    loop-less. Returns (service, conn, clock)."""
    service, conn, clock, _ = _setup(ttl=ttl)
    await _begin(service)
    return service, conn, clock


async def test_begin_status_stop_roundtrip() -> None:
    events = [{"t": 1, "kind": "click", "data": {"x": 1, "y": 2}}]
    service, conn, _, _ = _setup(_scripted_handler(events))
    entry = await _begin(service)
    assert entry.agent_recording_id == "agent-rec-1"
    assert conn.dispatched[0].inputs == {"action": "start", "max_events": 2000}

    _, view = await service.recording_status(tenant_id=TENANT, recording_id=entry.recording_id)
    assert view["event_count"] == 1

    _, parsed, truncated = await service.stop_recording(
        tenant_id=TENANT, recording_id=entry.recording_id
    )
    assert len(parsed) == 1 and parsed[0].kind == "click"
    assert truncated is False
    assert conn.dispatched[-1].inputs == {"action": "stop", "recording_id": "agent-rec-1"}
    # Entry is gone after stop.
    with pytest.raises(RecordingNotFound):
        await service.recording_status(tenant_id=TENANT, recording_id=entry.recording_id)


async def test_per_tenant_concurrency_bound() -> None:
    service, _, _, _ = _setup(max_per_tenant=2)
    await _begin(service, "one")
    await _begin(service, "two")
    with pytest.raises(RecordingLimitReached):
        await _begin(service, "three")
    assert len(service.list_active(TENANT)) == 2


async def test_expired_entries_are_swept_and_discarded_on_agent() -> None:
    service, conn, clock, _ = _setup(ttl=10.0)
    entry = await _begin(service)
    clock.now += 11.0
    assert await service.sweep_expired() == 1
    # The sweep told the agent to discard its buffer.
    assert conn.dispatched[-1].inputs == {"action": "discard", "recording_id": "agent-rec-1"}
    with pytest.raises(RecordingNotFound):
        await service.recording_status(tenant_id=TENANT, recording_id=entry.recording_id)


async def test_cross_tenant_lookup_is_not_found() -> None:
    service, _, _, _ = _setup()
    entry = await _begin(service)
    with pytest.raises(RecordingNotFound):
        await service.recording_status(
            tenant_id=uuid.uuid4(), recording_id=entry.recording_id
        )
    assert service.list_active(uuid.uuid4()) == []


async def test_failed_start_releases_the_slot() -> None:
    def broken(task):
        return RemoteResult(task_id=task.task_id, ok=True, outputs={"status": "recording"})

    service, _, _, _ = _setup(broken, max_per_tenant=1)
    with pytest.raises(AgentRecordingError, match="recording_id"):
        await _begin(service)
    # The reserved slot was released — a retry is allowed.
    with pytest.raises(AgentRecordingError):
        await _begin(service)


async def test_malformed_start_with_recording_id_discards_on_agent() -> None:
    # The agent returns ok=True with a recording_id but a contract-invalid
    # status, so the server rejects the start. If the agent began capturing
    # before sending that malformed reply, the slot is wedged until its own TTL
    # backstop unless the server discards it — so the server must salvage the
    # reported recording_id and dispatch a discard keyed on it.
    def handler(task):
        if task.inputs["action"] == "start":
            return RemoteResult(
                task_id=task.task_id,
                ok=True,
                outputs={"recording_id": "agent-rec-1", "status": "idle"},
            )
        return RemoteResult(
            task_id=task.task_id,
            ok=True,
            outputs={"recording_id": "agent-rec-1", "status": "discarded", "event_count": 0},
        )

    service, conn, _, _ = _setup(handler, max_per_tenant=1)
    with pytest.raises(AgentRecordingError, match="status"):
        await _begin(service)
    assert [t.inputs["action"] for t in conn.dispatched] == ["start", "discard"]
    assert conn.dispatched[-1].inputs == {"action": "discard", "recording_id": "agent-rec-1"}
    # The reserved slot was released — a retry is allowed.
    with pytest.raises(AgentRecordingError):
        await _begin(service)


async def test_malformed_start_without_recording_id_sends_no_discard() -> None:
    # When the agent omits the recording_id there is nothing to address a
    # discard at, so the server only releases the slot and does not dispatch a
    # discard frame.
    def handler(task):
        return RemoteResult(task_id=task.task_id, ok=True, outputs={"status": "recording"})

    service, conn, _, _ = _setup(handler, max_per_tenant=1)
    with pytest.raises(AgentRecordingError, match="recording_id"):
        await _begin(service)
    assert [t.inputs["action"] for t in conn.dispatched] == ["start"]


async def test_no_agent_is_a_clean_placement_error() -> None:
    service, _, _, _ = _setup()
    with pytest.raises(AgentUnavailable):
        await service.begin_recording(
            tenant_id=TENANT,
            created_by=USER,
            name="x",
            agent_alias="offline-agent",
            max_events=100,
        )


async def test_disabled_dispatcher_is_unavailable() -> None:
    service = RecordingService(dispatcher=None, agents=AgentRegistry())
    with pytest.raises(RecordingUnavailable):
        await _begin(service)


async def test_discard_survives_agent_failure() -> None:
    calls = {"n": 0}

    def handler(task):
        if task.inputs["action"] == "discard":
            calls["n"] += 1
            return RemoteResult(task_id=task.task_id, ok=False, error={"message": "gone"})
        return RemoteResult(
            task_id=task.task_id,
            ok=True,
            outputs={"recording_id": "agent-rec-1", "status": "recording", "event_count": 0},
        )

    service, _, _, _ = _setup(handler)
    entry = await _begin(service)
    # Agent-side discard fails, but the server entry still goes away.
    await service.discard_recording(tenant_id=TENANT, recording_id=entry.recording_id)
    assert calls["n"] == 1
    with pytest.raises(RecordingNotFound):
        await service.discard_recording(tenant_id=TENANT, recording_id=entry.recording_id)


# ---------- regression: expiry on request paths still discards on the agent ---


async def _settle() -> None:
    """Let opportunistically-scheduled drain tasks run to completion."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_expiry_seen_on_status_path_discards_on_agent() -> None:
    # An entry that expires and is then purged by a status/_get call (not the
    # sweep) must still get an agent-side discard, not be silently dropped.
    service, conn, clock, _ = _setup(ttl=10.0)
    entry = await _begin(service)
    clock.now += 11.0
    with pytest.raises(RecordingNotFound):
        await service.recording_status(tenant_id=TENANT, recording_id=entry.recording_id)
    await _settle()
    assert conn.dispatched[-1].inputs == {
        "action": "discard",
        "recording_id": "agent-rec-1",
    }
    # And the sweep doesn't double-discard what the request path already drained.
    assert await service.sweep_expired() == 0


async def test_expiry_seen_on_list_path_discards_on_agent() -> None:
    service, conn, clock, _ = _setup(ttl=10.0)
    await _begin(service)
    clock.now += 11.0
    assert service.list_active(TENANT) == []
    await _settle()
    assert conn.dispatched[-1].inputs == {
        "action": "discard",
        "recording_id": "agent-rec-1",
    }


async def test_expiry_seen_on_begin_path_discards_on_agent() -> None:
    # Starting a fresh recording purges an expired sibling; that sibling must
    # still be discarded on the agent.
    service, conn, clock, _ = _setup(ttl=10.0)
    await _begin(service, "first")
    clock.now += 11.0
    await _begin(service, "second")  # purges "first" under the lock
    await _settle()
    discards = [t for t in conn.dispatched if t.inputs["action"] == "discard"]
    assert len(discards) == 1


def test_list_active_from_non_loop_thread_defers_discard_to_sweep() -> None:
    # A sync FastAPI endpoint runs in a threadpool worker that has no running
    # event loop, so list_active's opportunistic _kick_drain no-ops there: an
    # entry that expires on the list path lands in _pending_discard and is only
    # discarded by the next sweep, ~60s later. This sync test reproduces that
    # real execution context (begin under a loop so the begin-time drain
    # completes, then list_active from a plain Thread with no loop alive) — it is
    # why list_recordings must be async, and the API test below asserts the async
    # endpoint drains immediately. Without the async endpoint, this gap is live.
    service, conn, clock = asyncio.run(_setup_and_begin())
    assert [t.inputs["action"] for t in conn.dispatched] == ["start"]

    clock.now += 11.0  # expire the entry
    result: dict[str, object] = {}

    def worker() -> None:
        result["active"] = service.list_active(TENANT)

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert result["active"] == []
    # No discard: the non-loop caller couldn't kick the drain, so the entry is
    # only queued for the next sweep.
    assert [t.inputs["action"] for t in conn.dispatched] == ["start"]
    assert len(service._pending_discard) == 1
    # The sweep (or the agent's own TTL) still covers it — no permanent leak.
    assert asyncio.run(service.sweep_expired()) == 1
    assert conn.dispatched[-1].inputs == {"action": "discard", "recording_id": "agent-rec-1"}


async def test_stop_lifecycle_discards_active_captures_on_agent() -> None:
    service, conn, _, _ = _setup()
    await _begin(service, "one")
    await _begin(service, "two")
    await service.stop()  # lifespan shutdown
    discards = [t for t in conn.dispatched if t.inputs["action"] == "discard"]
    assert len(discards) == 2
    # The registry is empty afterwards regardless of the agent's response.
    assert service.list_active(TENANT) == []


# ---------- regression: recording is pinned to the begin-time agent -----------


async def test_pool_resolved_target_is_rejected() -> None:
    # A target that resolves only via pool matching (no exact alias) must be
    # rejected so status/stop can't later land on a different pool member.
    service, _, _, agents = _setup()
    agents.register(_make_conn(_scripted_handler(), alias="lab-2", pools=("kiosk",)))
    with pytest.raises(AgentUnavailable, match="exact agent alias"):
        await service.begin_recording(
            tenant_id=TENANT,
            created_by=USER,
            name="x",
            agent_alias="kiosk",
            max_events=100,
        )


async def test_all_actions_target_the_pinned_alias() -> None:
    # Even with a second, alphabetically-earlier agent joining the same tenant
    # after start, every later action targets the exact alias we recorded on.
    service, conn, _, agents = _setup()
    entry = await _begin(service)
    assert entry.agent_alias == "lab-1"
    early = _make_conn(_scripted_handler(), alias="aaa-1")
    agents.register(early)
    await service.recording_status(tenant_id=TENANT, recording_id=entry.recording_id)
    await service.stop_recording(tenant_id=TENANT, recording_id=entry.recording_id)
    # The pinned agent saw every action; the newcomer saw none.
    assert [t.inputs["action"] for t in conn.dispatched] == ["start", "status", "stop"]
    assert early.dispatched == []


# ---------- regression: the agent's truncated flag is surfaced ----------------


async def test_stop_reports_truncated_flag() -> None:
    def handler(task):
        action = task.inputs["action"]
        if action == "stop":
            return RemoteResult(
                task_id=task.task_id,
                ok=True,
                outputs={
                    "recording_id": "agent-rec-1",
                    "status": "stopped",
                    "event_count": 1,
                    "truncated": True,
                    "events": [{"t": 1, "kind": "click", "data": {"x": 1, "y": 2}}],
                },
            )
        return RemoteResult(
            task_id=task.task_id,
            ok=True,
            outputs={"recording_id": "agent-rec-1", "status": "recording", "event_count": 0},
        )

    service, _, _, _ = _setup(handler)
    entry = await _begin(service)
    _, events, truncated = await service.stop_recording(
        tenant_id=TENANT, recording_id=entry.recording_id
    )
    assert truncated is True
    assert len(events) == 1
