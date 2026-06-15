"""Durability wiring, dry-run simulation, and governed HITL.

Covers the interpreter-coupled core wired in this stage:

  - CheckpointStore: the executor persists a per-layer checkpoint after each
    DAG layer settles (and mirrors the newest onto runs.checkpoint).
  - Resume: a ResumeState seeds env, skips already-completed nodes WITHOUT
    re-dispatching them or re-emitting their events, and runs the rest.
  - Orchestrator recovery: a RUNNING run WITH a checkpoint is resumed; one
    WITHOUT a checkpoint (or with no CheckpointStore) is failed.
  - Dry-run: side-effecting caps are simulated; read-only caps run for real.
  - HumanTaskStore: a human.prompt persists a PENDING task, resolves it on
    answer, and an escalation sweep flips a stale one to ESCALATED.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

from aakaar.db.models import (
    Base,
    HumanTask,
    HumanTaskStatus,
    Run,
    RunCheckpoint,
    RunEvent,
    RunMode,
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
from aakaar.interpreter.durability import CheckpointStore, ResumeState
from aakaar.interpreter.events import InMemoryEventRecorder
from aakaar.interpreter.human_tasks import HumanTaskStore, _as_aware
from aakaar.interpreter.orchestrator import RunOrchestrator
from aakaar.interpreter.signals import SignalHub
from aakaar.shared.dag.types import Dag, Edge, Node, NodeKind
from aakaar.shared.registry import (
    ActionDefinition,
    Registry,
    build_default_registry,
)

# ---------- helpers --------------------------------------------------------


def _sf(tmp_path: Path) -> SessionFactory:
    engine = make_engine(EngineConfig(url=f"sqlite:///{tmp_path/'d.sqlite'}"))
    Base.metadata.create_all(engine)
    return SessionFactory(engine)


class _IO(BaseModel):
    pass


def _registry_with_effect_flags() -> Registry:
    """A registry whose two refs differ only in `side_effecting`."""
    reg = build_default_registry()
    reg.add(
        ActionDefinition(
            ref="test.send",
            description="a side-effecting send",
            input_schema=_IO,
            output_schema=_IO,
            side_effecting=True,
        )
    )
    reg.add(
        ActionDefinition(
            ref="test.read",
            description="a read-only fetch",
            input_schema=_IO,
            output_schema=_IO,
            side_effecting=False,
        )
    )
    reg.add(
        ActionDefinition(
            ref="test.undeclared",
            description="forgot to declare side_effecting",
            input_schema=_IO,
            output_schema=_IO,
            # side_effecting defaults to None -> conservatively simulated.
        )
    )
    return reg


def _ctx(
    registry: Registry,
    *,
    mode: str = RunMode.LIVE,
    resume: ResumeState | None = None,
    tenant_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
) -> RunContext:
    tid = tenant_id or uuid.uuid4()
    rid = run_id or uuid.uuid4()
    actx = ActivityContext(
        tenant_id=tid,
        run_id=rid,
        registry=registry,
        object_store=None,  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
    )
    return RunContext(
        run_id=rid, tenant_id=tid, activity_ctx=actx, mode=mode, resume=resume
    )


# ---------- checkpoint persistence ----------------------------------------


@pytest.mark.asyncio
async def test_executor_saves_layer_checkpoints(tmp_path: Path) -> None:
    sf = _sf(tmp_path)
    activities = ActivityRegistry()

    async def ok(_actx, _inputs):
        return {"v": 1}

    activities.register("test.read", ok)
    # a -> b : two layers.
    dag = Dag(
        nodes=[
            Node(id="a", kind=NodeKind.ACTION, ref="test.read"),
            Node(id="b", kind=NodeKind.ACTION, ref="test.read"),
        ],
        edges=[Edge(source="a", target="b")],
    )
    # A runs row (with valid FKs) must exist for the runs.checkpoint mirror.
    rid, tid = _tenant_run(sf)

    ex = LocalExecutor(
        activities=activities,
        recorder=InMemoryEventRecorder(),
        signals=SignalHub(),
        checkpoints=CheckpointStore(session_factory=sf),
    )
    outcome = await ex.execute(dag, _ctx(build_default_registry(), tenant_id=tid, run_id=rid))
    assert outcome.status == "succeeded"

    with sf.session() as s:
        cps = (
            s.query(RunCheckpoint)
            .filter(RunCheckpoint.run_id == rid)
            .order_by(RunCheckpoint.layer_index)
            .all()
        )
        assert [c.layer_index for c in cps] == [0, 1]
        assert cps[0].completed_node_ids == ["a"]
        assert cps[1].completed_node_ids == ["b"]
        run = s.get(Run, rid)
        assert run is not None and run.checkpoint is not None
        assert run.checkpoint["layer_index"] == 1


# ---------- resume skips completed nodes ----------------------------------


@pytest.mark.asyncio
async def test_resume_skips_completed_nodes_no_redispatch() -> None:
    """A resumed run seeds env from the checkpoint, never re-dispatches a
    completed node (no handler call, no events), and runs only the rest."""
    activities = ActivityRegistry()
    calls: dict[str, int] = {"a": 0, "b": 0}

    async def make(name: str):
        async def handler(_actx, _inputs):
            calls[name] += 1
            return {"ran": name}

        return handler

    activities.register("a.ref", await make("a"))
    activities.register("b.ref", await make("b"))
    dag = Dag(
        nodes=[
            Node(id="a", kind=NodeKind.ACTION, ref="a.ref"),
            Node(id="b", kind=NodeKind.ACTION, ref="b.ref"),
        ],
        edges=[Edge(source="a", target="b")],
    )
    recorder = InMemoryEventRecorder()
    ex = LocalExecutor(activities=activities, recorder=recorder, signals=SignalHub())
    # Resume as if layer 0 (node a) already settled: seed its output, resume at
    # layer 1, mark a completed.
    resume = ResumeState(
        next_layer_index=1,
        env={"a": {"ran": "a"}},
        completed_ids=frozenset({"a"}),
    )
    ctx = _ctx(build_default_registry(), resume=resume)
    outcome = await ex.execute(dag, ctx)

    assert outcome.status == "succeeded"
    # Node a must NOT have run again; node b runs once.
    assert calls == {"a": 0, "b": 1}
    # Final env still carries the seeded output for a.
    assert outcome.outputs["a"] == {"ran": "a"}
    assert outcome.outputs["b"] == {"ran": "b"}
    # No events were emitted for a (not re-dispatched); b has a node_started.
    events = recorder.events.get(ctx.run_id, [])
    a_events = [e for e in events if e.node_id == "a"]
    b_events = [e for e in events if e.node_id == "b"]
    assert a_events == []
    assert any(e.kind == "node_started" for e in b_events)


@pytest.mark.asyncio
async def test_resume_partial_layer_finishes_remaining_sibling() -> None:
    """If a layer had two siblings and only one settled before the crash, the
    resumed run finishes the other without re-running the done one."""
    activities = ActivityRegistry()
    calls: dict[str, int] = {"x": 0, "y": 0}

    def make(name: str):
        async def handler(_actx, _inputs):
            calls[name] += 1
            return {"ran": name}

        return handler

    activities.register("x.ref", make("x"))
    activities.register("y.ref", make("y"))
    # x and y are both in layer 0 (no edges) — siblings.
    dag = Dag(
        nodes=[
            Node(id="x", kind=NodeKind.ACTION, ref="x.ref"),
            Node(id="y", kind=NodeKind.ACTION, ref="y.ref"),
        ],
    )
    ex = LocalExecutor(
        activities=activities, recorder=InMemoryEventRecorder(), signals=SignalHub()
    )
    # Resume INTO layer 0 (next_layer_index=0) with x already done.
    resume = ResumeState(
        next_layer_index=0,
        env={"x": {"ran": "x"}},
        completed_ids=frozenset({"x"}),
    )
    outcome = await ex.execute(dag, _ctx(build_default_registry(), resume=resume))
    assert outcome.status == "succeeded"
    assert calls == {"x": 0, "y": 1}
    assert outcome.outputs["x"] == {"ran": "x"}
    assert outcome.outputs["y"] == {"ran": "y"}


# ---------- orchestrator recovery -----------------------------------------


def _seed_run_with_checkpoint(
    sf: SessionFactory, *, with_checkpoint: bool
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a RUNNING run for a real 2-node DAG, optionally with a layer-0
    checkpoint so it is resumable."""
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
        dag = {
            "id": "",
            "version": 0,
            "nodes": [
                {"id": "a", "kind": "action", "ref": "a.ref"},
                {"id": "b", "kind": "action", "ref": "b.ref"},
            ],
            "edges": [{"from": "a", "to": "b"}],
        }
        s.add(
            WorkflowVersion(
                tenant_id=t.id,
                workflow_id=wf.id,
                version=1,
                dag=dag,
                created_by=u.id,
            )
        )
        s.flush()
        run = Run(
            tenant_id=t.id,
            workflow_id=wf.id,
            workflow_version=1,
            started_by=u.id,
            status=RunStatus.RUNNING,
            inputs={},
        )
        if with_checkpoint:
            run.checkpoint = {
                "layer_index": 0,
                "completed_node_ids": ["a"],
                "env": {"a": {"ran": "a"}},
            }
        s.add(run)
        s.flush()
        if with_checkpoint:
            s.add(
                RunCheckpoint(
                    tenant_id=t.id,
                    run_id=run.id,
                    layer_index=0,
                    completed_node_ids=["a"],
                    env={"a": {"ran": "a"}},
                )
            )
        s.commit()
        return run.id, t.id


@pytest.mark.asyncio
async def test_recover_resumes_run_with_checkpoint(tmp_path: Path) -> None:
    """A RUNNING run WITH a checkpoint is resumed (not failed): node a is
    skipped, node b runs, the run succeeds, resume_count is bumped, and a
    resume event is recorded."""
    sf = _sf(tmp_path)
    run_id, tenant_id = _seed_run_with_checkpoint(sf, with_checkpoint=True)

    activities = ActivityRegistry()
    calls: dict[str, int] = {"a": 0, "b": 0}

    def make(name: str):
        async def handler(_actx, _inputs):
            calls[name] += 1
            return {"ran": name}

        return handler

    activities.register("a.ref", make("a"))
    activities.register("b.ref", make("b"))
    recorder = InMemoryEventRecorder()
    signals = SignalHub()
    checkpoints = CheckpointStore(session_factory=sf)
    executor = LocalExecutor(
        activities=activities, recorder=recorder, signals=signals, checkpoints=checkpoints
    )
    orch = RunOrchestrator(
        session_factory=sf,
        executor=executor,
        signals=signals,
        recorder=recorder,
        registry=build_default_registry(),
        object_store=None,  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
        checkpoints=checkpoints,
    )
    n = orch.recover_interrupted_runs()
    assert n == 1
    # The resume scheduled a task; wait for it.
    outcome = await orch.wait_for(run_id)
    assert outcome.status == "succeeded"
    # a was already done (skipped); only b ran.
    assert calls == {"a": 0, "b": 1}
    with sf.session() as s:
        run = s.get(Run, run_id)
        assert run is not None
        assert run.status == RunStatus.SUCCEEDED
        assert run.resume_count == 1
    # A resume event was recorded.
    kinds = [e.kind for e in recorder.events.get(run_id, [])]
    assert "run_resumed_ckpt" in kinds


def test_recover_fails_run_without_checkpoint(tmp_path: Path) -> None:
    """A RUNNING run with NO checkpoint is failed even when a CheckpointStore
    is wired (nothing to resume from)."""
    sf = _sf(tmp_path)
    run_id, _tenant_id = _seed_run_with_checkpoint(sf, with_checkpoint=False)
    orch = RunOrchestrator(
        session_factory=sf,
        executor=None,  # type: ignore[arg-type]
        signals=SignalHub(),
        recorder=InMemoryEventRecorder(),
        registry=build_default_registry(),
        object_store=None,  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
        checkpoints=CheckpointStore(session_factory=sf),
    )
    n = orch.recover_interrupted_runs()
    assert n == 1
    with sf.session() as s:
        run = s.get(Run, run_id)
        assert run is not None
        assert run.status == RunStatus.FAILED
        assert run.error and run.error.get("type") == "Interrupted"


def test_recover_respects_resume_cap(tmp_path: Path) -> None:
    """A checkpointed run that has already hit max_resumes is failed, not
    resumed forever (poison-run guard)."""
    sf = _sf(tmp_path)
    run_id, _tenant_id = _seed_run_with_checkpoint(sf, with_checkpoint=True)
    with sf.session() as s:
        run = s.get(Run, run_id)
        assert run is not None
        run.resume_count = 5
        s.commit()
    orch = RunOrchestrator(
        session_factory=sf,
        executor=None,  # type: ignore[arg-type]
        signals=SignalHub(),
        recorder=InMemoryEventRecorder(),
        registry=build_default_registry(),
        object_store=None,  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
        checkpoints=CheckpointStore(session_factory=sf),
        max_resumes=5,
    )
    n = orch.recover_interrupted_runs()
    assert n == 1
    with sf.session() as s:
        run = s.get(Run, run_id)
        assert run is not None
        assert run.status == RunStatus.FAILED


# ---------- dry-run --------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_simulates_side_effecting_runs_readonly() -> None:
    reg = _registry_with_effect_flags()
    activities = ActivityRegistry()
    ran: list[str] = []

    async def send(_actx, _inputs):
        ran.append("send")
        return {"sent": True}

    async def read(_actx, _inputs):
        ran.append("read")
        return {"data": 42}

    activities.register("test.send", send)
    activities.register("test.read", read)
    dag = Dag(
        nodes=[
            Node(id="r", kind=NodeKind.ACTION, ref="test.read"),
            Node(id="s", kind=NodeKind.ACTION, ref="test.send"),
        ],
    )
    ex = LocalExecutor(
        activities=activities, recorder=InMemoryEventRecorder(), signals=SignalHub()
    )
    outcome = await ex.execute(dag, _ctx(reg, mode=RunMode.DRY_RUN))
    assert outcome.status == "succeeded"
    # Read-only ran for real; side-effecting was simulated (handler skipped).
    assert ran == ["read"]
    assert outcome.outputs["r"] == {"data": 42}
    assert outcome.outputs["s"] == {"simulated": True, "would_run": "test.send"}


@pytest.mark.asyncio
async def test_dry_run_simulates_undeclared_capability() -> None:
    """A cap that forgot to declare side_effecting (None) is conservatively
    simulated in dry-run so it can never move money during a simulation."""
    reg = _registry_with_effect_flags()
    activities = ActivityRegistry()
    ran: list[str] = []

    async def undeclared(_actx, _inputs):
        ran.append("undeclared")
        return {"did": "real-work"}

    activities.register("test.undeclared", undeclared)
    dag = Dag(nodes=[Node(id="u", kind=NodeKind.ACTION, ref="test.undeclared")])
    ex = LocalExecutor(
        activities=activities, recorder=InMemoryEventRecorder(), signals=SignalHub()
    )
    outcome = await ex.execute(dag, _ctx(reg, mode=RunMode.DRY_RUN))
    assert outcome.status == "succeeded"
    assert ran == []  # never executed
    assert outcome.outputs["u"] == {"simulated": True, "would_run": "test.undeclared"}


@pytest.mark.asyncio
async def test_live_mode_executes_side_effecting() -> None:
    """The same side-effecting cap runs for real in live mode."""
    reg = _registry_with_effect_flags()
    activities = ActivityRegistry()
    ran: list[str] = []

    async def send(_actx, _inputs):
        ran.append("send")
        return {"sent": True}

    activities.register("test.send", send)
    dag = Dag(nodes=[Node(id="s", kind=NodeKind.ACTION, ref="test.send")])
    ex = LocalExecutor(
        activities=activities, recorder=InMemoryEventRecorder(), signals=SignalHub()
    )
    outcome = await ex.execute(dag, _ctx(reg, mode=RunMode.LIVE))
    assert outcome.status == "succeeded"
    assert ran == ["send"]
    assert outcome.outputs["s"] == {"sent": True}


# ---------- governed HITL --------------------------------------------------


def _tenant_run(sf: SessionFactory) -> tuple[uuid.UUID, uuid.UUID]:
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
        run = Run(
            tenant_id=t.id,
            workflow_id=wf.id,
            workflow_version=1,
            started_by=u.id,
            status=RunStatus.RUNNING,
            inputs={},
        )
        s.add(run)
        s.commit()
        return run.id, t.id


def test_human_task_open_resolve(tmp_path: Path) -> None:
    sf = _sf(tmp_path)
    run_id, tenant_id = _tenant_run(sf)
    store = HumanTaskStore(session_factory=sf, sla_seconds=3600, escalation_seconds=1800)
    store.open(
        run_id=run_id,
        tenant_id=tenant_id,
        node_id="n1",
        message="Approve the wire?",
        expects="confirm",
        timeout_seconds=7200,
    )
    with sf.session() as s:
        row = s.query(HumanTask).filter(HumanTask.run_id == run_id).one()
        assert row.status == HumanTaskStatus.PENDING
        assert row.deadline_at is not None and row.escalation_at is not None
        # escalation precedes deadline.
        assert row.escalation_at < row.deadline_at

    store.resolve(run_id=run_id, node_id="n1", response="yes")
    with sf.session() as s:
        row = s.query(HumanTask).filter(HumanTask.run_id == run_id).one()
        assert row.status == HumanTaskStatus.RESPONDED
        assert row.response == "yes"
        assert row.responded_at is not None


def test_human_task_otp_response_redacted(tmp_path: Path) -> None:
    sf = _sf(tmp_path)
    run_id, tenant_id = _tenant_run(sf)
    store = HumanTaskStore(session_factory=sf)
    store.open(
        run_id=run_id, tenant_id=tenant_id, node_id="n1", message="OTP?", expects="otp"
    )
    store.resolve(run_id=run_id, node_id="n1", response="123456")
    with sf.session() as s:
        row = s.query(HumanTask).filter(HumanTask.run_id == run_id).one()
        assert row.status == HumanTaskStatus.RESPONDED
        assert "123456" not in (row.response or "")
        assert "redacted" in (row.response or "")


def test_human_task_sla_clamped_to_prompt_timeout(tmp_path: Path) -> None:
    """A short prompt timeout clamps the SLA so a task can't outlive its
    waiting coroutine."""
    sf = _sf(tmp_path)
    run_id, tenant_id = _tenant_run(sf)
    store = HumanTaskStore(session_factory=sf, sla_seconds=3600, escalation_seconds=1800)
    before = datetime.now(UTC)
    store.open(
        run_id=run_id,
        tenant_id=tenant_id,
        node_id="n1",
        message="quick",
        expects="text",
        timeout_seconds=30,
    )
    with sf.session() as s:
        row = s.query(HumanTask).filter(HumanTask.run_id == run_id).one()
        assert row.deadline_at is not None
        # deadline within ~30s, not an hour (SQLite drops tzinfo on read-back).
        deadline = _as_aware(row.deadline_at)
        assert deadline is not None and deadline <= before + timedelta(seconds=60)


def test_human_task_escalation_sweep(tmp_path: Path) -> None:
    sf = _sf(tmp_path)
    run_id, tenant_id = _tenant_run(sf)
    recorder = InMemoryEventRecorder()
    store = HumanTaskStore(session_factory=sf, recorder=recorder)
    # Open a task whose escalation_at is already in the past (deadline still
    # in the future, so it escalates rather than expires).
    now = datetime.now(UTC)
    with sf.session() as s:
        s.add(
            HumanTask(
                tenant_id=tenant_id,
                run_id=run_id,
                node_id="n1",
                prompt="stale",
                expects="text",
                status=HumanTaskStatus.PENDING,
                escalation_at=now - timedelta(minutes=5),
                deadline_at=now + timedelta(hours=1),
            )
        )
        s.commit()

    escalated = store.sweep_escalations()
    assert escalated == 1
    with sf.session() as s:
        row = s.query(HumanTask).filter(HumanTask.run_id == run_id).one()
        assert row.status == HumanTaskStatus.ESCALATED
    # An escalation event was recorded.
    kinds = [e.kind for e in recorder.events.get(run_id, [])]
    assert "run_paused" in kinds
    payloads = [e.payload for e in recorder.events.get(run_id, [])]
    assert any(p.get("reason") == "human_prompt_escalated" for p in payloads)

    # Idempotent: a second sweep does not re-escalate.
    assert store.sweep_escalations() == 0


def test_human_task_escalation_sweep_expires_past_deadline(tmp_path: Path) -> None:
    sf = _sf(tmp_path)
    run_id, tenant_id = _tenant_run(sf)
    store = HumanTaskStore(session_factory=sf)
    now = datetime.now(UTC)
    with sf.session() as s:
        s.add(
            HumanTask(
                tenant_id=tenant_id,
                run_id=run_id,
                node_id="n1",
                prompt="dead",
                expects="text",
                status=HumanTaskStatus.PENDING,
                escalation_at=now - timedelta(hours=2),
                deadline_at=now - timedelta(hours=1),
            )
        )
        s.commit()
    # Past deadline -> EXPIRED (not escalated).
    assert store.sweep_escalations() == 0
    with sf.session() as s:
        row = s.query(HumanTask).filter(HumanTask.run_id == run_id).one()
        assert row.status == HumanTaskStatus.EXPIRED


# ---------- HITL end-to-end through the executor --------------------------


@pytest.mark.asyncio
async def test_executor_human_prompt_persists_and_resolves_task(tmp_path: Path) -> None:
    """A human.prompt node, with a HumanTaskStore wired, persists a PENDING task
    on open and marks it RESPONDED when the SignalHub future resolves — without
    breaking the existing SignalHub flow."""
    sf = _sf(tmp_path)
    run_id, tenant_id = _tenant_run(sf)
    signals = SignalHub()
    store = HumanTaskStore(session_factory=sf)
    ex = LocalExecutor(
        activities=ActivityRegistry(),
        recorder=InMemoryEventRecorder(),
        signals=signals,
        human_tasks=store,
    )
    dag = Dag(
        nodes=[
            Node(
                id="ask",
                kind=NodeKind.CONTROL,
                ref="human.prompt",
                inputs={"message": "ok?", "expects": "text", "timeout_seconds": 30},
            )
        ]
    )
    ctx = _ctx(build_default_registry(), tenant_id=tenant_id, run_id=run_id)
    task = asyncio.ensure_future(ex.execute(dag, ctx))

    # Wait until the prompt is registered, then assert a PENDING task exists.
    for _ in range(200):
        if signals.list_pending(run_id):
            break
        await asyncio.sleep(0.005)
    assert signals.list_pending(run_id), "prompt never opened"
    with sf.session() as s:
        row = s.query(HumanTask).filter(HumanTask.run_id == run_id).one()
        assert row.status == HumanTaskStatus.PENDING

    await signals.resolve(run_id, "ask", "yes")
    outcome = await task
    assert outcome.status == "succeeded"
    assert outcome.outputs["ask"] == {"response": "yes"}
    with sf.session() as s:
        row = s.query(HumanTask).filter(HumanTask.run_id == run_id).one()
        assert row.status == HumanTaskStatus.RESPONDED


@pytest.mark.asyncio
async def test_outbox_marks_events_published_and_sweep_replays(tmp_path: Path) -> None:
    """The outbox-backed recorder marks rows published after dispatch, and a
    sweep replays anything left unpublished (at-least-once)."""
    from aakaar.interpreter.durability import EventOutbox
    from aakaar.interpreter.events import DbEventRecorder
    from aakaar.services.events import EventBroker, OutboxEventRecorder

    sf = _sf(tmp_path)
    run_id, tenant_id = _tenant_run(sf)
    broker = EventBroker()
    delivered: list[dict] = []
    q = broker.subscribe(run_id)

    outbox = EventOutbox(session_factory=sf, publish_fn=broker.publish)
    recorder = OutboxEventRecorder(DbEventRecorder(session_factory=sf), outbox)
    recorder.record(
        run_id=run_id, tenant_id=tenant_id, node_id=None, kind="log", payload={"a": 1}
    )
    # Drain the broker queue.
    while not q.empty():
        delivered.append(q.get_nowait())
    assert len(delivered) == 1
    # The row is marked published (dispatch succeeded).
    with sf.session() as s:
        row = s.query(RunEvent).filter(RunEvent.run_id == run_id).one()
        assert row.published is True
        assert row.published_at is not None

    # Simulate a crash-recorded unpublished row, then sweep.
    with sf.session() as s:
        s.add(
            RunEvent(
                tenant_id=tenant_id,
                run_id=run_id,
                sequence=99,
                node_id=None,
                kind="log",
                payload={"replayed": True},
                published=False,
            )
        )
        s.commit()
    replayed = outbox.sweep()
    assert replayed == 1
    with sf.session() as s:
        row = (
            s.query(RunEvent)
            .filter(RunEvent.run_id == run_id, RunEvent.sequence == 99)
            .one()
        )
        assert row.published is True


@pytest.mark.asyncio
async def test_escalator_loop_escalates_then_stops(tmp_path: Path) -> None:
    """The periodic escalator runs a sweep on its loop and stops cleanly."""
    from aakaar.interpreter.human_tasks import HumanTaskEscalator

    sf = _sf(tmp_path)
    run_id, tenant_id = _tenant_run(sf)
    now = datetime.now(UTC)
    with sf.session() as s:
        s.add(
            HumanTask(
                tenant_id=tenant_id,
                run_id=run_id,
                node_id="n1",
                prompt="stale",
                expects="text",
                status=HumanTaskStatus.PENDING,
                escalation_at=now - timedelta(minutes=5),
                deadline_at=now + timedelta(hours=1),
            )
        )
        s.commit()
    store = HumanTaskStore(session_factory=sf, recorder=InMemoryEventRecorder())
    esc = HumanTaskEscalator(store=store, tick_seconds=0.02)
    await esc.start()
    # Give the loop a couple of ticks to run the first sweep.
    for _ in range(100):
        with sf.session() as s:
            row = s.query(HumanTask).filter(HumanTask.run_id == run_id).one()
            if row.status == HumanTaskStatus.ESCALATED:
                break
        await asyncio.sleep(0.01)
    await esc.stop()
    with sf.session() as s:
        row = s.query(HumanTask).filter(HumanTask.run_id == run_id).one()
        assert row.status == HumanTaskStatus.ESCALATED
    # Idempotent stop.
    await esc.stop()
