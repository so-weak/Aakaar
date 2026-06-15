"""Tamper-evident audit ledger: chained writes, verify, and export.

Two layers:

1. Service-level tests drive :class:`AuditRecorder` + the ledger functions
   directly against a SQLite DB. They prove the hash chain is actually written
   (seq + prev_hash + entry_hash), that a clean chain verifies, and that
   tamper / gap / cross-tenant cases behave. These never touch ``create_app``
   so they always run.

2. HTTP tests mount *only* the audit router on a bare FastAPI app and override
   the four dependencies it uses (auth + session + deps). This exercises the
   real routing, authz guards (403/404), and the streamed export without
   pulling in the full application wiring.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from aakaar.api.deps import (
    get_deps,
    get_session,
    require_superuser,
    require_tenant_admin,
)
from aakaar.api.routers import audit as audit_router
from aakaar.db.models import AuditLog, Base, Tenant
from aakaar.db.session import EngineConfig, SessionFactory, make_engine
from aakaar.services.audit import AuditFileSink, AuditRecorder
from aakaar.services.audit.chain import GENESIS_PREV, compute_entry_hash
from aakaar.services.audit.ledger import iter_chain, verify_chain

# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------


def _sf(tmp_path: Path) -> SessionFactory:
    engine = make_engine(EngineConfig(url=f"sqlite:///{tmp_path / 'audit.sqlite'}"))
    Base.metadata.create_all(engine)
    return SessionFactory(engine)


def _make_tenant(sf: SessionFactory, slug: str = "t1") -> uuid.UUID:
    with sf.session() as s:
        t = Tenant(slug=slug, name=slug.upper())
        s.add(t)
        s.commit()
        return t.id


def _record_n(rec: AuditRecorder, tenant_id: uuid.UUID, n: int) -> None:
    for i in range(n):
        rec.record(
            action=f"act.{i}",
            tenant_id=tenant_id,
            target_kind="user",
            target_id=f"u{i}",
            payload={"i": i, "note": f"event-{i}"},
        )


# --------------------------------------------------------------------------
# service-level: the chain is actually written
# --------------------------------------------------------------------------


def test_recorder_writes_hash_chain(tmp_path: Path) -> None:
    """Each tenant row gets a monotonic seq, a genesis NULL prev_hash, and a
    linked entry_hash — i.e. the recorder is not silently dropping the chain."""
    sf = _sf(tmp_path)
    tid = _make_tenant(sf)
    rec = AuditRecorder(session_factory=sf, sink=AuditFileSink(tmp_path))
    _record_n(rec, tid, 4)

    with sf.session() as s:
        rows = list(iter_chain(s, tenant_id=tid))
    assert [r.seq for r in rows] == [1, 2, 3, 4]
    assert rows[0].prev_hash is None  # genesis carries NULL
    assert all(r.entry_hash for r in rows)
    # Each row links to its predecessor's entry_hash.
    for i in range(1, len(rows)):
        assert rows[i].prev_hash == rows[i - 1].entry_hash
    # Stored hashes match an independent recomputation.
    prev: str | None = None
    for r in rows:
        assert r.seq is not None
        expect = compute_entry_hash(
            prev_hash=r.prev_hash,
            tenant_id=tid,
            seq=r.seq,
            actor_id=r.actor_id,
            action=r.action,
            target_kind=r.target_kind,
            target_id=r.target_id,
            at=r.at,
            payload=r.payload,
        )
        assert r.entry_hash == expect
        assert (r.prev_hash or GENESIS_PREV) == (prev or GENESIS_PREV)
        prev = r.entry_hash


def test_system_rows_are_unchained(tmp_path: Path) -> None:
    """tenant_id=None (bootstrap/superuser) rows stay an unverifiable side log:
    NULL seq, no hash — they must not enter any tenant's chain."""
    sf = _sf(tmp_path)
    rec = AuditRecorder(session_factory=sf)
    rec.record(action="system.boot", tenant_id=None, target_kind="system", target_id="-")

    with sf.session() as s:
        rows = list(s.scalars(select(AuditLog)))
    assert len(rows) == 1
    assert rows[0].seq is None
    assert rows[0].entry_hash is None


def test_verify_clean_chain(tmp_path: Path) -> None:
    sf = _sf(tmp_path)
    tid = _make_tenant(sf)
    _record_n(AuditRecorder(session_factory=sf), tid, 6)
    with sf.session() as s:
        res = verify_chain(s, tenant_id=tid)
    assert res.intact is True
    assert res.count == 6
    assert res.first_seq == 1
    assert res.last_seq == 6
    assert res.broken_at is None


def test_verify_empty_chain_is_ok(tmp_path: Path) -> None:
    sf = _sf(tmp_path)
    tid = _make_tenant(sf)
    with sf.session() as s:
        res = verify_chain(s, tenant_id=tid)
    assert res.intact is True
    assert res.count == 0
    assert res.broken_at is None


def test_verify_detects_tampered_payload(tmp_path: Path) -> None:
    """Mutating one row's payload directly in the DB must be caught, and the
    reported first_broken_seq must be that exact row."""
    sf = _sf(tmp_path)
    tid = _make_tenant(sf)
    _record_n(AuditRecorder(session_factory=sf), tid, 5)
    # Tamper seq 3's payload in place (no rehash) — exactly what an attacker
    # editing the row would leave behind.
    with sf.session() as s:
        row = s.scalars(
            select(AuditLog).where(AuditLog.tenant_id == tid, AuditLog.seq == 3)
        ).one()
        row.payload = {"i": 3, "note": "tampered!"}
        s.commit()

    with sf.session() as s:
        res = verify_chain(s, tenant_id=tid)
    assert res.intact is False
    assert res.broken_at == 3
    assert res.reason is not None and "entry_hash" in res.reason


def test_verify_detects_gap_from_deleted_row(tmp_path: Path) -> None:
    """Deleting a middle row leaves a seq gap; verify reports the first seq
    that is missing its predecessor."""
    sf = _sf(tmp_path)
    tid = _make_tenant(sf)
    _record_n(AuditRecorder(session_factory=sf), tid, 5)
    with sf.session() as s:
        row = s.scalars(
            select(AuditLog).where(AuditLog.tenant_id == tid, AuditLog.seq == 3)
        ).one()
        s.delete(row)
        s.commit()

    with sf.session() as s:
        res = verify_chain(s, tenant_id=tid)
    assert res.intact is False
    # seq 3 is gone, so when we reach seq 4 the expected seq (3) mismatches.
    assert res.broken_at == 4
    assert res.reason is not None and "gap" in res.reason.lower()


def test_chains_are_per_tenant_isolated(tmp_path: Path) -> None:
    """Two tenants each get an independent chain starting at seq 1; tampering
    one does not implicate the other."""
    sf = _sf(tmp_path)
    t1 = _make_tenant(sf, "t1")
    t2 = _make_tenant(sf, "t2")
    rec = AuditRecorder(session_factory=sf)
    _record_n(rec, t1, 3)
    _record_n(rec, t2, 3)
    with sf.session() as s:
        r1 = list(iter_chain(s, tenant_id=t1))
        r2 = list(iter_chain(s, tenant_id=t2))
    assert [r.seq for r in r1] == [1, 2, 3]
    assert [r.seq for r in r2] == [1, 2, 3]
    # Break t1; t2 stays intact.
    with sf.session() as s:
        row = s.scalars(
            select(AuditLog).where(AuditLog.tenant_id == t1, AuditLog.seq == 2)
        ).one()
        row.action = "act.hacked"
        s.commit()
    with sf.session() as s:
        assert verify_chain(s, tenant_id=t1).intact is False
        assert verify_chain(s, tenant_id=t2).intact is True


def _reverify_export(lines: list[str]) -> tuple[bool, int | None]:
    """Independently re-verify an exported JSONL chain the way an external
    auditor would: recompute each entry_hash from the prior, check links.
    Returns (ok, first_broken_seq)."""
    from datetime import datetime

    prev_hash = GENESIS_PREV
    expected_seq: int | None = None
    for raw in lines:
        rec = json.loads(raw)
        seq = rec["seq"]
        if expected_seq is None:
            expected_seq = seq
        if seq != expected_seq:
            return False, seq
        if (rec["prev_hash"] or GENESIS_PREV) != prev_hash:
            return False, seq
        recomputed = compute_entry_hash(
            prev_hash=None if rec["prev_hash"] == GENESIS_PREV else rec["prev_hash"],
            tenant_id=uuid.UUID(rec["tenant_id"]),
            seq=seq,
            actor_id=uuid.UUID(rec["actor_id"]) if rec["actor_id"] else None,
            action=rec["action"],
            target_kind=rec["target_kind"],
            target_id=rec["target_id"],
            at=datetime.fromisoformat(rec["at"]),
            payload=rec["payload"],
        )
        if recomputed != rec["entry_hash"]:
            return False, seq
        prev_hash = rec["entry_hash"]
        expected_seq = seq + 1
    return True, None


# --------------------------------------------------------------------------
# HTTP layer: mount only the audit router with overridden deps
# --------------------------------------------------------------------------


class _FakeUser:
    def __init__(self, tenant_id: uuid.UUID | None) -> None:
        self.tenant_id = tenant_id


class _FakeDeps:
    def __init__(self, sf: SessionFactory) -> None:
        self.session_factory = sf


@pytest.fixture()
def audit_app(tmp_path: Path) -> Iterator[tuple[FastAPI, SessionFactory, uuid.UUID]]:
    sf = _sf(tmp_path)
    tid = _make_tenant(sf, "acme")
    _record_n(AuditRecorder(session_factory=sf), tid, 5)

    app = FastAPI()
    app.include_router(audit_router.router)

    def _session_override() -> Iterator[Session]:
        s = sf.session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_deps] = lambda: _FakeDeps(sf)
    # Default identity: a tenant_admin for `tid`. Individual tests re-point
    # these overrides to exercise authz.
    app.dependency_overrides[require_tenant_admin] = lambda: _FakeUser(tid)
    app.dependency_overrides[require_superuser] = lambda: _FakeUser(None)
    yield app, sf, tid


def test_http_verify_ok(audit_app: tuple[FastAPI, SessionFactory, uuid.UUID]) -> None:
    app, _sf_, _tid = audit_app
    with TestClient(app) as c:
        r = c.get("/audit/verify")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["entries_checked"] == 5
    assert body["first_broken_seq"] is None


def test_http_verify_detects_tamper(
    audit_app: tuple[FastAPI, SessionFactory, uuid.UUID],
) -> None:
    app, sf, tid = audit_app
    with sf.session() as s:
        row = s.scalars(
            select(AuditLog).where(AuditLog.tenant_id == tid, AuditLog.seq == 2)
        ).one()
        row.payload = {"i": 2, "note": "evil"}
        s.commit()
    with TestClient(app) as c:
        r = c.get("/audit/verify")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["first_broken_seq"] == 2


def test_http_export_roundtrips_and_reverifies(
    audit_app: tuple[FastAPI, SessionFactory, uuid.UUID],
) -> None:
    app, _sf_, tid = audit_app
    with TestClient(app) as c:
        r = c.get("/audit/export")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/x-ndjson")
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    assert len(lines) == 5
    # Every exported line carries the chain material an auditor needs.
    first = json.loads(lines[0])
    assert set(first) >= {
        "seq",
        "tenant_id",
        "action",
        "at",
        "payload",
        "prev_hash",
        "entry_hash",
    }
    assert first["prev_hash"] == GENESIS_PREV  # genesis emitted explicitly
    assert first["tenant_id"] == str(tid)
    ok, broken = _reverify_export(lines)
    assert ok is True and broken is None


def test_http_export_detects_tamper_offline(
    audit_app: tuple[FastAPI, SessionFactory, uuid.UUID],
) -> None:
    """An auditor who tampers with an exported line (or who exported a tampered
    DB) sees the offline re-verify fail at the right seq."""
    app, sf, tid = audit_app
    with sf.session() as s:
        row = s.scalars(
            select(AuditLog).where(AuditLog.tenant_id == tid, AuditLog.seq == 4)
        ).one()
        row.action = "act.forged"
        s.commit()
    with TestClient(app) as c:
        lines = [ln for ln in c.get("/audit/export").text.splitlines() if ln.strip()]
    ok, broken = _reverify_export(lines)
    assert ok is False
    assert broken == 4


def test_http_verify_requires_tenant_admin(
    audit_app: tuple[FastAPI, SessionFactory, uuid.UUID],
) -> None:
    """A non-admin principal is rejected by the guard (403)."""
    app, _sf_, _tid = audit_app

    def _forbidden() -> _FakeUser:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="tenant admin only")

    app.dependency_overrides[require_tenant_admin] = _forbidden
    with TestClient(app) as c:
        assert c.get("/audit/verify").status_code == 403
        assert c.get("/audit/export").status_code == 403


def test_http_superuser_verify_unknown_tenant_404(
    audit_app: tuple[FastAPI, SessionFactory, uuid.UUID],
) -> None:
    app, _sf_, _tid = audit_app
    missing = uuid.uuid4()
    with TestClient(app) as c:
        r = c.get(f"/audit/tenants/{missing}/verify")
    assert r.status_code == 404


def test_http_superuser_can_verify_any_tenant(
    audit_app: tuple[FastAPI, SessionFactory, uuid.UUID],
) -> None:
    app, _sf_, tid = audit_app
    with TestClient(app) as c:
        r = c.get(f"/audit/tenants/{tid}/verify")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["entries_checked"] == 5
