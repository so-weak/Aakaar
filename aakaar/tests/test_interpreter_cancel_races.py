"""Executor-level regression tests for cancel timing races.

These mirror the orchestrator's exact cancel sequence — request_cancel() on
the handle, then signals.cancel_all_for() — at the deterministic moments where
the prior implementation silently lost the cancel:

  1. cancel lands in the window AFTER the layer checkpoint but BEFORE a
     human.prompt has registered its pending signal (cancel_all_for pops
     nothing; the prompt then registers and would sit out the full timeout,
     finishing FAILED rather than CANCELLED);
  2. a human.prompt shares a layer with a slow capability node, and a cancel
     mid-layer must let the sibling settle before the cancelled outcome is
     returned — no node_completed may appear after the run unwinds.

They drive `LocalExecutor.execute` directly with a `RunControlHandle` so the
timing is exact and doesn't depend on HTTP polling.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from aakaar.db.models import (
    Base,
    Run,
    RunEventKind,
    RunStatus,
    Tenant,
    User,
    UserRole,
    UserStatus,
    Workflow,
    WorkflowVersion,
)
from aakaar.db.session import EngineConfig, SessionFactory, make_engine
from aakaar.interpreter import LocalExecutor, RunContext
from aakaar.interpreter.activities.registry import ActivityRegistry
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.interpreter.controls import ControlHub, RunCancelled, RunControlHandle
from aakaar.interpreter.events import InMemoryEventRecorder
from aakaar.interpreter.orchestrator import RunOrchestrator
from aakaar.interpreter.signals import SignalHub
from aakaar.shared.dag.types import Dag, Edge, Node, NodeKind
from aakaar.shared.registry import build_default_registry


def _ctx_with_controls(
    *, registry, signals: SignalHub
) -> tuple[RunContext, RunControlHandle]:
    tenant_id = uuid.uuid4()
    run_id = uuid.uuid4()
    activity_ctx = ActivityContext(
        tenant_id=tenant_id,
        run_id=run_id,
        registry=registry,
        object_store=None,  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
    )
    handle = ControlHub().register(run_id, tenant_id)
    ctx = RunContext(
        run_id=run_id, tenant_id=tenant_id, activity_ctx=activity_ctx, controls=handle
    )
    return ctx, handle


async def _cancel(handle: RunControlHandle, signals: SignalHub, run_id: uuid.UUID) -> None:
    """Replay the orchestrator's cancel sequence exactly."""
    handle.request_cancel()
    await signals.cancel_all_for(run_id)


@pytest.mark.asyncio
async def test_cancel_before_prompt_registers_is_not_lost() -> None:
    """Cancel arriving in the checkpoint->open window must still CANCEL, fast.

    Previously cancel_all_for found no pending prompt (the prompt had not
    registered yet) and popped nothing; the prompt then awaited the full
    300s timeout and the run finished FAILED. The fix watches cancel_event
    directly, so a cancel requested before the await wins immediately.
    """
    signals = SignalHub()
    dag = Dag(
        nodes=[
            Node(
                id="ask",
                kind=NodeKind.CONTROL,
                ref="human.prompt",
                # A large timeout: if the cancel were lost we'd hang on it.
                inputs={"message": "?", "timeout_seconds": 300},
            )
        ]
    )
    recorder = InMemoryEventRecorder()
    executor = LocalExecutor(
        activities=ActivityRegistry(), recorder=recorder, signals=signals
    )
    ctx, handle = _ctx_with_controls(registry=build_default_registry(), signals=signals)

    task = asyncio.create_task(executor.execute(dag, ctx))
    # One slice: the layer checkpoint passed, the prompt-node task has been
    # created but has NOT yet reached `await self.signals.open(...)`. This is
    # exactly the window the finding reproduces.
    await asyncio.sleep(0)
    assert signals.list_pending(ctx.run_id) == [], "prompt must not have registered yet"

    await _cancel(handle, signals, ctx.run_id)

    outcome = await asyncio.wait_for(task, timeout=2.0)
    assert outcome.status == "cancelled"
    assert outcome.error is None
    # Contract: a cancelled run never emits node_failed.
    kinds = [e.kind for e in recorder.events.get(ctx.run_id, [])]
    assert RunEventKind.NODE_FAILED not in kinds
    # And the prompt is no longer respond-able.
    assert signals.list_pending(ctx.run_id) == []


@pytest.mark.asyncio
async def test_cancel_while_prompt_pending_interrupts_immediately() -> None:
    """Baseline race: cancel after the prompt registers still unwinds CANCELLED
    without waiting out the timeout (covers the orchestrator's normal path)."""
    signals = SignalHub()
    dag = Dag(
        nodes=[
            Node(
                id="ask",
                kind=NodeKind.CONTROL,
                ref="human.prompt",
                inputs={"message": "?", "timeout_seconds": 300},
            )
        ]
    )
    recorder = InMemoryEventRecorder()
    executor = LocalExecutor(
        activities=ActivityRegistry(), recorder=recorder, signals=signals
    )
    ctx, handle = _ctx_with_controls(registry=build_default_registry(), signals=signals)

    task = asyncio.create_task(executor.execute(dag, ctx))
    for _ in range(100):
        await asyncio.sleep(0.005)
        if signals.list_pending(ctx.run_id):
            break
    assert signals.list_pending(ctx.run_id), "prompt should have registered"

    await _cancel(handle, signals, ctx.run_id)
    outcome = await asyncio.wait_for(task, timeout=2.0)
    assert outcome.status == "cancelled"
    assert RunEventKind.NODE_FAILED not in [
        e.kind for e in recorder.events.get(ctx.run_id, [])
    ]


@pytest.mark.asyncio
async def test_cancel_mid_layer_lets_slow_sibling_settle_first() -> None:
    """A prompt + a slow sibling in one layer: a cancel must NOT return the
    cancelled outcome (and hand control back to the orchestrator for teardown)
    until the sibling coroutine has actually finished. No node_completed may be
    recorded after execute() returns."""
    signals = SignalHub()
    activities = ActivityRegistry()
    sibling_finished = asyncio.Event()

    async def slow_sibling(_actx, _inputs):
        # Long enough that, without the settle fix, the cancelled outcome
        # would be returned well before this finishes.
        await asyncio.sleep(0.3)
        sibling_finished.set()
        return {"done": True}

    activities.register("browser.navigate", slow_sibling)

    dag = Dag(
        nodes=[
            Node(
                id="ask",
                kind=NodeKind.CONTROL,
                ref="human.prompt",
                inputs={"message": "?", "timeout_seconds": 300},
            ),
            Node(
                id="sib",
                kind=NodeKind.ACTION,
                ref="browser.navigate",
                inputs={"session": "s", "url": "u"},
            ),
        ]
        # No edges: both nodes are in the same (first) layer.
    )
    recorder = InMemoryEventRecorder()
    executor = LocalExecutor(activities=activities, recorder=recorder, signals=signals)
    ctx, handle = _ctx_with_controls(registry=build_default_registry(), signals=signals)

    task = asyncio.create_task(executor.execute(dag, ctx))
    for _ in range(100):
        await asyncio.sleep(0.005)
        if signals.list_pending(ctx.run_id):
            break
    assert signals.list_pending(ctx.run_id), "prompt should have registered"
    assert not sibling_finished.is_set(), "sibling still running when we cancel"

    await _cancel(handle, signals, ctx.run_id)
    outcome = await asyncio.wait_for(task, timeout=2.0)

    assert outcome.status == "cancelled"
    # The sibling must have finished before execute() returned.
    assert sibling_finished.is_set(), "cancel returned before sibling settled"

    # The sibling's node_completed must NOT appear after the run unwinds. Since
    # the orchestrator records run_cancelled only after execute() returns, no
    # node_completed for the sibling may follow that point — here we assert the
    # sibling completed and was captured in the event stream before return.
    kinds = [e.kind for e in recorder.events.get(ctx.run_id, [])]
    assert kinds.count(RunEventKind.NODE_COMPLETED) >= 1
    assert RunEventKind.NODE_FAILED not in kinds


@pytest.mark.asyncio
async def test_failed_node_lets_slow_sibling_settle_first() -> None:
    """The same settle guarantee applies to an ordinary node failure: a fast
    failure in one node must not return before its slow peer finishes."""
    activities = ActivityRegistry()
    sibling_finished = asyncio.Event()

    async def boom(_actx, _inputs):
        raise RuntimeError("kaboom")

    async def slow_sibling(_actx, _inputs):
        await asyncio.sleep(0.25)
        sibling_finished.set()
        return {}

    activities.register("browser.navigate", boom)
    activities.register("browser.click", slow_sibling)

    dag = Dag(
        nodes=[
            Node(
                id="bad", kind=NodeKind.ACTION, ref="browser.navigate",
                inputs={"session": "s", "url": "u"},
            ),
            Node(
                id="sib", kind=NodeKind.ACTION, ref="browser.click",
                inputs={"session": "s", "selector": "x"},
            ),
        ]
    )
    recorder = InMemoryEventRecorder()
    executor = LocalExecutor(
        activities=activities, recorder=recorder, signals=SignalHub()
    )
    ctx, _ = _ctx_with_controls(registry=build_default_registry(), signals=SignalHub())

    outcome = await executor.execute(dag, ctx)
    assert outcome.status == "failed"
    assert outcome.error and "kaboom" in outcome.error["message"]
    assert sibling_finished.is_set(), "failure returned before slow sibling settled"


@pytest.mark.asyncio
async def test_cancel_at_layer_boundary_still_unwinds() -> None:
    """Regression guard for the existing checkpoint path: a two-layer DAG
    cancelled between layers raises RunCancelled at the checkpoint and never
    starts layer 2."""
    activities = ActivityRegistry()
    layer2_ran = asyncio.Event()

    async def first(_actx, _inputs):
        return {}

    async def second(_actx, _inputs):
        layer2_ran.set()
        return {}

    activities.register("browser.open_session", first)
    activities.register("browser.navigate", second)

    dag = Dag(
        nodes=[
            Node(id="one", kind=NodeKind.ACTION, ref="browser.open_session"),
            Node(
                id="two", kind=NodeKind.ACTION, ref="browser.navigate",
                inputs={"prev": "${one}", "session": "s", "url": "u"},
            ),
        ],
        edges=[Edge.model_validate({"from": "one", "to": "two"})],
    )
    recorder = InMemoryEventRecorder()
    signals = SignalHub()
    executor = LocalExecutor(activities=activities, recorder=recorder, signals=signals)
    ctx, handle = _ctx_with_controls(registry=build_default_registry(), signals=signals)

    # Cancel before execute even starts: the very first checkpoint trips it.
    await _cancel(handle, signals, ctx.run_id)
    outcome = await executor.execute(dag, ctx)
    assert outcome.status == "cancelled"
    assert not layer2_ran.is_set()


@pytest.mark.asyncio
async def test_hard_cancelled_sibling_settles_layer_as_cancelled() -> None:
    """A node task hard-cancelled (not the cooperative RunCancelled path) must
    still let `_run_layer` settle the whole layer and surface a RunCancelled —
    it must NOT leak a bare CancelledError out of `task.exception()`.

    Drives `_run_layer` directly so the per-node tasks are reachable: we cancel
    one while its slow peer is still running and assert the peer settles first
    and `_run_layer` raises RunCancelled (which `execute` maps to 'cancelled').
    """
    activities = ActivityRegistry()
    peer_finished = asyncio.Event()
    victim_started = asyncio.Event()

    async def victim(_actx, _inputs):
        victim_started.set()
        await asyncio.sleep(5)  # hard-cancelled before this returns
        return {}

    async def peer(_actx, _inputs):
        await asyncio.sleep(0.2)
        peer_finished.set()
        return {"ok": True}

    activities.register("browser.navigate", victim)
    activities.register("browser.click", peer)

    recorder = InMemoryEventRecorder()
    signals = SignalHub()
    executor = LocalExecutor(activities=activities, recorder=recorder, signals=signals)
    ctx, _ = _ctx_with_controls(registry=build_default_registry(), signals=signals)

    layer = [
        Node(id="v", kind=NodeKind.ACTION, ref="browser.navigate",
             inputs={"session": "s", "url": "u"}),
        Node(id="p", kind=NodeKind.ACTION, ref="browser.click",
             inputs={"session": "s", "selector": "x"}),
    ]

    captured: list[asyncio.Task] = []
    real_ensure_future = asyncio.ensure_future

    def _capture(coro, **kw):
        t = real_ensure_future(coro, **kw)
        captured.append(t)
        return t

    import unittest.mock

    with unittest.mock.patch.object(asyncio, "ensure_future", _capture):
        layer_task = asyncio.create_task(
            executor._run_layer(layer, env={}, alias_to_id={"v": "v", "p": "p"}, ctx=ctx)
        )
        await victim_started.wait()
        # Cancel the victim node's task. `_run_layer` ensure_futures the node
        # coroutines in layer order, so the first captured `_run_node` task is
        # the victim ("v"). Filter by coro name to ignore any internal wrappers.
        node_tasks = [
            t for t in captured if t.get_coro().cr_code.co_name == "_run_node"  # type: ignore[union-attr]
        ]
        node_tasks[0].cancel()

        with pytest.raises(RunCancelled):
            await asyncio.wait_for(layer_task, timeout=2.0)

    # The slow peer settled before _run_layer surfaced the cancellation.
    assert peer_finished.is_set()
    kinds = [e.kind for e in recorder.events.get(ctx.run_id, [])]
    assert RunEventKind.NODE_FAILED not in kinds


# ---------- orchestrator-level: terminal/cleanup ordering -------------------


def _orch_sf(tmp_path: Path) -> SessionFactory:
    engine = make_engine(EngineConfig(url=f"sqlite:///{tmp_path / 'cancel.sqlite'}"))
    Base.metadata.create_all(engine)
    return SessionFactory(engine)


def _seed_run(sf: SessionFactory) -> tuple[uuid.UUID, uuid.UUID]:
    with sf.session() as s:
        t = Tenant(slug="t1", name="T1")
        s.add(t)
        s.flush()
        u = User(
            tenant_id=t.id,
            email="a@b.test",
            password_hash="x",
            role=UserRole.TENANT_USER,
            status=UserStatus.ACTIVE,
        )
        s.add(u)
        s.flush()
        wf = Workflow(tenant_id=t.id, created_by=u.id, name="w", latest_version=1)
        s.add(wf)
        s.flush()
        s.add(
            WorkflowVersion(
                tenant_id=t.id, workflow_id=wf.id, version=1, dag={"nodes": []},
                created_by=u.id,
            )
        )
        run = Run(
            tenant_id=t.id,
            workflow_id=wf.id,
            workflow_version=1,
            started_by=u.id,
            status=RunStatus.QUEUED,
            inputs={},
        )
        s.add(run)
        s.commit()
        return t.id, run.id


@pytest.mark.asyncio
async def test_orchestrator_cancel_settles_sibling_before_terminal(tmp_path: Path) -> None:
    """End-to-end through the orchestrator: a prompt + slow sibling in one
    layer, cancelled mid-flight, must record run_cancelled AFTER the sibling's
    node_completed, and must not tear down session_state until the sibling has
    finished touching it.
    """
    sf = _orch_sf(tmp_path)
    tenant_id, run_id = _seed_run(sf)

    signals = SignalHub()
    recorder = InMemoryEventRecorder()
    activities = ActivityRegistry()
    sibling_done = asyncio.Event()
    closed_at: list[bool] = []  # records sibling_done state at close() time

    class _Session:
        def close(self) -> None:
            # The orchestrator's session_state cleanup closes live handles.
            # If it runs while the sibling is still going, sibling_done is
            # not yet set — that's the bug Finding 3 describes.
            closed_at.append(sibling_done.is_set())

    async def slow_sibling(actx, _inputs):
        await asyncio.sleep(0.3)
        sibling_done.set()
        # Stash a closeable handle so the orchestrator runs cleanup on it.
        actx.session_state["browser:main"] = _Session()
        return {"ok": True}

    activities.register("browser.navigate", slow_sibling)

    executor = LocalExecutor(activities=activities, recorder=recorder, signals=signals)
    orch = RunOrchestrator(
        session_factory=sf,
        executor=executor,
        signals=signals,
        recorder=recorder,
        registry=build_default_registry(),
        object_store=None,  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
    )

    dag = Dag(
        nodes=[
            Node(
                id="ask", kind=NodeKind.CONTROL, ref="human.prompt",
                inputs={"message": "?", "timeout_seconds": 300},
            ),
            Node(
                id="sib", kind=NodeKind.ACTION, ref="browser.navigate",
                inputs={"session": "s", "url": "u"},
            ),
        ]
    )
    orch.schedule(run_id=run_id, tenant_id=tenant_id, dag=dag, granted_caps={})

    for _ in range(200):
        await asyncio.sleep(0.005)
        if signals.list_pending(run_id):
            break
    assert signals.list_pending(run_id), "prompt should have registered"
    assert not sibling_done.is_set()

    await orch.cancel_run(run_id=run_id)
    outcome = await asyncio.wait_for(orch.wait_for(run_id), timeout=3.0)
    assert outcome.status == "cancelled"

    # Terminal status persisted as CANCELLED, with an ended_at.
    with sf.session() as s:
        row = s.get(Run, run_id)
        assert row is not None
        assert row.status == RunStatus.CANCELLED
        assert row.ended_at is not None

    # The sibling settled, and session_state cleanup saw it as done.
    assert sibling_done.is_set()
    assert closed_at == [True], "session cleanup ran before the sibling finished"

    kinds = [e.kind for e in recorder.events.get(run_id, [])]
    assert RunEventKind.NODE_FAILED not in kinds
    # run_cancelled is the terminal event and lands AFTER the sibling's
    # node_completed — the ordering Finding 3 said was inverted.
    assert RunEventKind.RUN_CANCELLED in kinds
    last_completed = max(
        i for i, e in enumerate(recorder.events[run_id])
        if e.kind == RunEventKind.NODE_COMPLETED
    )
    cancelled_at = next(
        i for i, e in enumerate(recorder.events[run_id])
        if e.kind == RunEventKind.RUN_CANCELLED
    )
    assert last_completed < cancelled_at
