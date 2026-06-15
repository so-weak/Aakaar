"""Retention sweep, legal hold, and right-to-erasure — the compliance core.

Proves the guarantees a regulator cares about: expired PII is erased, a legal
hold blocks erasure, erasure leaves a tombstone but never touches the audit
trail, and the whole thing is idempotent.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from aakaar.api.repositories import audit as audit_repo
from aakaar.db.models import (
    Base,
    Run,
    RunEvent,
    RunStatus,
    Tenant,
    User,
    UserRole,
    UserStatus,
    Workflow,
    WorkflowVersion,
)
from aakaar.db.session import EngineConfig, SessionFactory, make_engine
from aakaar.services.audit import AuditRecorder
from aakaar.services.retention.service import (
    RESOURCE_RUN,
    LegalHoldError,
    RetentionService,
)


class _FakeStore:
    """Minimal ObjectStorage stand-in — retention only calls delete()."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, uri: str) -> None:
        self.deleted.append(uri)


def _sf(tmp_path: Path) -> SessionFactory:
    engine = make_engine(EngineConfig(url=f"sqlite:///{tmp_path / 'r.sqlite'}"))
    Base.metadata.create_all(engine)
    return SessionFactory(engine)


def _svc(sf: SessionFactory) -> RetentionService:
    return RetentionService(
        session_factory=sf,
        object_store=_FakeStore(),  # type: ignore[arg-type]
        audit=AuditRecorder(session_factory=sf),
    )


def _seed_run(sf: SessionFactory, *, ended_days_ago: int) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a tenant with one finished run that ended `ended_days_ago` days ago."""
    ended = datetime.now(UTC) - timedelta(days=ended_days_ago)
    with sf.session() as s:
        t = Tenant(slug="t1", name="T1")
        s.add(t)
        s.flush()
        u = User(
            tenant_id=t.id, email="a@b.test", password_hash="x",
            role=UserRole.TENANT_USER, status=UserStatus.ACTIVE,
        )
        s.add(u)
        s.flush()
        wf = Workflow(tenant_id=t.id, created_by=u.id, name="w", latest_version=1)
        s.add(wf)
        s.flush()
        s.add(WorkflowVersion(
            tenant_id=t.id, workflow_id=wf.id, version=1, dag={"nodes": []}, created_by=u.id,
        ))
        s.flush()
        run = Run(
            tenant_id=t.id, workflow_id=wf.id, workflow_version=1, started_by=u.id,
            status=RunStatus.SUCCEEDED, inputs={"ssn": "123-45-6789"},
            outputs={"balance": "secret"}, started_at=ended, ended_at=ended,
        )
        s.add(run)
        s.flush()
        s.add(RunEvent(
            tenant_id=t.id, run_id=run.id, sequence=1, node_id="n1",
            kind="node_completed", payload={"ssn": "123-45-6789"}, published=True,
        ))
        s.commit()
        return t.id, run.id


def test_sweep_erases_expired_run_and_scrubs_pii(tmp_path: Path) -> None:
    sf = _sf(tmp_path)
    tid, run_id = _seed_run(sf, ended_days_ago=40)
    svc = _svc(sf)
    svc.upsert_policy(tenant_id=tid, resource_type=RESOURCE_RUN, ttl_days=30, updated_by=None)

    report = svc.sweep(tenant_id=tid)
    assert report.erased == 1

    with sf.session() as s:
        run = s.get(Run, run_id)
        assert run is not None
        assert run.erased_at is not None              # tombstone, not deleted
        assert run.inputs == {"_erased": True}        # PII scrubbed
        assert run.outputs == {"_erased": True}
        ev = s.scalars(select(RunEvent).where(RunEvent.run_id == run_id)).one()
        assert ev.payload == {"_erased": True}        # denormalized PII scrubbed too


def test_sweep_skips_legal_hold(tmp_path: Path) -> None:
    sf = _sf(tmp_path)
    tid, run_id = _seed_run(sf, ended_days_ago=40)
    svc = _svc(sf)
    svc.upsert_policy(tenant_id=tid, resource_type=RESOURCE_RUN, ttl_days=30, updated_by=None)
    svc.set_legal_hold(tenant_id=tid, resource_type=RESOURCE_RUN, resource_id=run_id, hold=True)

    report = svc.sweep(tenant_id=tid)
    # A held run is filtered out before it is even a sweep candidate, so the
    # guarantee is simply: nothing erased, and the run survives intact.
    assert report.erased == 0
    with sf.session() as s:
        run = s.get(Run, run_id)
        assert run is not None and run.erased_at is None  # preserved under hold
        assert run.inputs == {"ssn": "123-45-6789"}       # PII untouched


def test_explicit_erase_refused_under_legal_hold(tmp_path: Path) -> None:
    sf = _sf(tmp_path)
    tid, run_id = _seed_run(sf, ended_days_ago=1)
    svc = _svc(sf)
    svc.set_legal_hold(tenant_id=tid, resource_type=RESOURCE_RUN, resource_id=run_id, hold=True)
    with pytest.raises(LegalHoldError):
        svc.erase_resource(tenant_id=tid, resource_type=RESOURCE_RUN, resource_id=run_id)


def test_ttl_null_retains_forever(tmp_path: Path) -> None:
    sf = _sf(tmp_path)
    tid, run_id = _seed_run(sf, ended_days_ago=9999)
    svc = _svc(sf)
    svc.upsert_policy(tenant_id=tid, resource_type=RESOURCE_RUN, ttl_days=None, updated_by=None)
    report = svc.sweep(tenant_id=tid)
    assert report.erased == 0
    with sf.session() as s:
        assert s.get(Run, run_id).erased_at is None  # type: ignore[union-attr]


def test_erasure_preserves_and_extends_audit_trail(tmp_path: Path) -> None:
    sf = _sf(tmp_path)
    tid, run_id = _seed_run(sf, ended_days_ago=1)
    svc = _svc(sf)
    with sf.session() as s:
        before = len(audit_repo.list_for_tenant(s, tenant_id=tid))

    svc.erase_resource(tenant_id=tid, resource_type=RESOURCE_RUN, resource_id=run_id, reason="dsr")

    with sf.session() as s:
        rows = audit_repo.list_for_tenant(s, tenant_id=tid)
    # The erasure itself is audited — the trail GREW, it was never erased.
    assert any(r.action == "retention.erased" for r in rows)
    assert len(rows) > before


def test_erase_is_idempotent(tmp_path: Path) -> None:
    sf = _sf(tmp_path)
    tid, run_id = _seed_run(sf, ended_days_ago=1)
    svc = _svc(sf)
    first = svc.erase_resource(tenant_id=tid, resource_type=RESOURCE_RUN, resource_id=run_id)
    second = svc.erase_resource(tenant_id=tid, resource_type=RESOURCE_RUN, resource_id=run_id)
    assert first.already_erased is False
    assert second.already_erased is True
