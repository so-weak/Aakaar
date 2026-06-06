"""Phase 3: workflow scheduler (one-off + cron due-ness) and the in-process
event broker / broadcasting recorder."""

from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aakaar.api.repositories import schedules as schedules_repo
from aakaar.db.models import (
    Base,
    Tenant,
    User,
    UserRole,
    UserStatus,
    Workflow,
    WorkflowSchedule,
    WorkflowVersion,
)
from aakaar.db.session import EngineConfig, SessionFactory, make_engine
from aakaar.interpreter.events import InMemoryEventRecorder
from aakaar.services.events import BroadcastingEventRecorder, EventBroker
from aakaar.services.scheduler import Scheduler
from aakaar.services.scheduler.scheduler import _DueSchedule


def _sf(tmp_path: Path) -> SessionFactory:
    engine = make_engine(EngineConfig(url=f"sqlite:///{tmp_path/'t.sqlite'}"))
    Base.metadata.create_all(engine)
    return SessionFactory(engine)


def _seed(sf: SessionFactory) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
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
                tenant_id=t.id, workflow_id=wf.id, version=1, dag={"nodes": []}, created_by=u.id
            )
        )
        s.commit()
        return t.id, u.id, wf.id


class _FakeOrch:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def schedule(self, *, run_id, tenant_id, dag, granted_caps, run_target=None):  # type: ignore[no-untyped-def]
        self.calls.append((run_id, tenant_id, dag, granted_caps, run_target))


@pytest.mark.asyncio
async def test_scheduler_fires_due_oneoff(tmp_path: Path) -> None:
    sf = _sf(tmp_path)
    tid, uid, wfid = _seed(sf)
    with sf.session() as s:
        schedules_repo.create_schedule(
            s,
            tenant_id=tid,
            workflow_id=wfid,
            created_by=uid,
            cron=None,
            scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
            inputs={"k": "v"},
        )
        s.commit()

    orch = _FakeOrch()
    sched = Scheduler(session_factory=sf, orchestrator=orch, tick_seconds=1.0)
    launched = await sched.tick_once()
    assert launched == 1
    assert len(orch.calls) == 1
    # One-off is disabled + stamped after firing; a second tick does nothing.
    with sf.session() as s:
        row = list(s.query(WorkflowSchedule))[0]
        assert row.enabled is False
        assert row.last_triggered_at is not None
    assert await sched.tick_once() == 0


@pytest.mark.asyncio
async def test_scheduler_skips_future_oneoff(tmp_path: Path) -> None:
    sf = _sf(tmp_path)
    tid, uid, wfid = _seed(sf)
    with sf.session() as s:
        schedules_repo.create_schedule(
            s,
            tenant_id=tid,
            workflow_id=wfid,
            created_by=uid,
            cron=None,
            scheduled_at=datetime.now(UTC) + timedelta(hours=1),
            inputs={},
        )
        s.commit()
    orch = _FakeOrch()
    sched = Scheduler(session_factory=sf, orchestrator=orch, tick_seconds=1.0)
    assert await sched.tick_once() == 0


def test_is_due_cron() -> None:
    now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC)

    class _S:
        id = uuid.uuid4()
        scheduled_at = None
        cron = "*/5 * * * *"  # every 5 minutes
        last_triggered_at = None
        created_at = datetime(2026, 6, 3, 11, 0, 0, tzinfo=UTC)

    assert Scheduler._is_due(_S(), now) is True

    class _S2(_S):
        last_triggered_at = datetime(2026, 6, 3, 11, 59, 0, tzinfo=UTC)

    # next fire after 11:59 is 12:00 -> due at now=12:00
    assert Scheduler._is_due(_S2(), now) is True

    class _S3(_S):
        last_triggered_at = datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC)

    # next fire after 12:00 is 12:05 -> not due at 12:00
    assert Scheduler._is_due(_S3(), now) is False


def test_event_broker_pub_sub() -> None:
    broker = EventBroker()
    rid = uuid.uuid4()
    q = broker.subscribe(rid)
    broker.publish(rid, {"sequence": 1, "kind": "node_started"})
    assert q.get_nowait() == {"sequence": 1, "kind": "node_started"}
    broker.unsubscribe(rid, q)
    # No subscribers now — publish is a no-op (does not raise).
    broker.publish(rid, {"sequence": 2, "kind": "node_completed"})


def test_broadcasting_recorder_publishes_and_persists() -> None:
    broker = EventBroker()
    inner = InMemoryEventRecorder()
    rec = BroadcastingEventRecorder(inner=inner, broker=broker)
    rid = uuid.uuid4()
    tid = uuid.uuid4()
    q = broker.subscribe(rid)
    evt = rec.record(
        run_id=rid, tenant_id=tid, node_id="n1", kind="node_completed", payload={"x": 1}
    )
    assert evt.sequence == 0
    assert inner.events[rid][0].kind == "node_completed"
    pushed = q.get_nowait()
    assert pushed["kind"] == "node_completed"
    assert pushed["node_id"] == "n1"
    assert pushed["payload"] == {"x": 1}


def test_due_schedule_dataclass_is_frozen() -> None:
    d = _DueSchedule(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        workflow_id=uuid.uuid4(),
        created_by=None,
        inputs={},
        is_oneoff=True,
    )
    with pytest.raises(FrozenInstanceError):
        d.is_oneoff = False  # type: ignore[misc]


@pytest.mark.asyncio
async def test_scheduler_forwards_run_target(tmp_path: Path) -> None:
    sf = _sf(tmp_path)
    tid, uid, wfid = _seed(sf)
    with sf.session() as s:
        schedules_repo.create_schedule(
            s,
            tenant_id=tid,
            workflow_id=wfid,
            created_by=uid,
            cron=None,
            scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
            inputs={},
            target="lab-1",
        )
        s.commit()
    orch = _FakeOrch()
    sched = Scheduler(session_factory=sf, orchestrator=orch, tick_seconds=1.0)
    assert await sched.tick_once() == 1
    # calls tuple = (run_id, tenant_id, dag, granted_caps, run_target)
    assert orch.calls[0][4] == "lab-1"
