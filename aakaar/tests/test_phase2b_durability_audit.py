"""Phase 2b: executor per-node retries, crash-safe run recovery, and the
audit recorder (redaction + persistence)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from aakaar.api.repositories import audit as audit_repo
from aakaar.db.models import (
    Base,
    Run,
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
from aakaar.interpreter.events import InMemoryEventRecorder
from aakaar.interpreter.orchestrator import RunOrchestrator
from aakaar.interpreter.signals import SignalHub
from aakaar.services.audit import AuditFileSink, AuditRecorder
from aakaar.shared.dag.types import Dag, Node, NodeKind, RetrySpec
from aakaar.shared.registry import build_default_registry


def _ctx(registry) -> RunContext:
    tenant_id = uuid.uuid4()
    run_id = uuid.uuid4()
    actx = ActivityContext(
        tenant_id=tenant_id,
        run_id=run_id,
        registry=registry,
        object_store=None,  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
    )
    return RunContext(run_id=run_id, tenant_id=tenant_id, activity_ctx=actx)


@pytest.mark.asyncio
async def test_executor_retries_then_succeeds() -> None:
    activities = ActivityRegistry()
    calls = {"n": 0}

    async def flaky(_actx, _inputs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return {"ok": True}

    activities.register("test.flaky", flaky)
    dag = Dag(
        nodes=[
            Node(
                id="a",
                kind=NodeKind.ACTION,
                ref="test.flaky",
                retry=RetrySpec(max_attempts=3, backoff_ms=0),
            )
        ]
    )
    recorder = InMemoryEventRecorder()
    ex = LocalExecutor(activities=activities, recorder=recorder, signals=SignalHub())
    ctx = _ctx(build_default_registry())
    outcome = await ex.execute(dag, ctx)
    assert outcome.status == "succeeded"
    assert calls["n"] == 3
    retrying = [
        e for e in recorder.events.get(ctx.run_id, []) if e.kind == "node_retrying"
    ]
    assert len(retrying) == 2


@pytest.mark.asyncio
async def test_executor_without_retry_fails_after_one_attempt() -> None:
    activities = ActivityRegistry()
    calls = {"n": 0}

    async def always_fails(_actx, _inputs):
        calls["n"] += 1
        raise RuntimeError("boom")

    activities.register("test.boom", always_fails)
    dag = Dag(nodes=[Node(id="a", kind=NodeKind.ACTION, ref="test.boom")])
    ex = LocalExecutor(
        activities=activities, recorder=InMemoryEventRecorder(), signals=SignalHub()
    )
    outcome = await ex.execute(dag, _ctx(build_default_registry()))
    assert outcome.status == "failed"
    assert calls["n"] == 1


def _sf(tmp_path: Path) -> SessionFactory:
    engine = make_engine(EngineConfig(url=f"sqlite:///{tmp_path/'t.sqlite'}"))
    Base.metadata.create_all(engine)
    return SessionFactory(engine)


def test_audit_recorder_redacts_and_persists(tmp_path: Path) -> None:
    sf = _sf(tmp_path)
    with sf.session() as s:
        t = Tenant(slug="t1", name="T1")
        s.add(t)
        s.commit()
        tid = t.id

    rec = AuditRecorder(session_factory=sf, sink=AuditFileSink(tmp_path))
    rec.record(
        action="auth.login",
        tenant_id=tid,
        target_kind="user",
        target_id="u1",
        payload={"password": "hunter2", "role": "tenant_admin"},
    )

    with sf.session() as s:
        rows = audit_repo.list_for_tenant(s, tenant_id=tid)
    assert len(rows) == 1
    assert rows[0].action == "auth.login"
    assert rows[0].payload["password"] == "<redacted>"
    assert rows[0].payload["role"] == "tenant_admin"
    # File sink mirrored the record.
    assert (tmp_path / "audit" / "audit.jsonl").read_text().strip()


def test_recover_interrupted_runs_marks_failed(tmp_path: Path) -> None:
    sf = _sf(tmp_path)
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
        run_id = run.id

    orch = RunOrchestrator(
        session_factory=sf,
        executor=None,  # type: ignore[arg-type]
        signals=None,  # type: ignore[arg-type]
        recorder=InMemoryEventRecorder(),
        registry=None,  # type: ignore[arg-type]
        object_store=None,  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
    )
    n = orch.recover_interrupted_runs()
    assert n == 1
    with sf.session() as s:
        refreshed = s.get(Run, run_id)
        assert refreshed is not None
        assert refreshed.status == RunStatus.FAILED
        assert refreshed.error and refreshed.error.get("type") == "Interrupted"
