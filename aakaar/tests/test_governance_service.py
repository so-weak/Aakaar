"""Maker-checker governance service: segregation-of-duties enforcement."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from aakaar.db.models import (
    ApprovalStatus,
    Base,
    Tenant,
    User,
    UserRole,
    UserStatus,
)
from aakaar.db.session import EngineConfig, SessionFactory, make_engine
from aakaar.services.governance import (
    GatedAction,
    GovernanceError,
    GovernanceService,
    SelfApprovalError,
    SubjectGoneError,
    workflow_is_gated,
)


def _sf(tmp_path: Path) -> SessionFactory:
    engine = make_engine(EngineConfig(url=f"sqlite:///{tmp_path/'g.sqlite'}"))
    Base.metadata.create_all(engine)
    return SessionFactory(engine)


def _tenant_two_users(sf: SessionFactory) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    with sf.session() as s:
        t = Tenant(slug="t1", name="T1")
        s.add(t)
        s.flush()
        maker = User(
            tenant_id=t.id,
            email="maker@b.test",
            password_hash="x",
            role=UserRole.TENANT_USER,
            status=UserStatus.ACTIVE,
        )
        checker = User(
            tenant_id=t.id,
            email="checker@b.test",
            password_hash="x",
            role=UserRole.TENANT_ADMIN,
            status=UserStatus.ACTIVE,
        )
        s.add_all([maker, checker])
        s.commit()
        return t.id, maker.id, checker.id


def _action() -> GatedAction:
    return GatedAction(
        subject_type="run_start",
        subject_ref=str(uuid.uuid4()),
        context={"workflow": "wire-transfer", "sensitivity": "elevated"},
    )


def test_workflow_is_gated() -> None:
    assert workflow_is_gated(requires_approval=True, sensitivity="normal")
    assert workflow_is_gated(requires_approval=False, sensitivity="elevated")
    assert not workflow_is_gated(requires_approval=False, sensitivity="normal")


def test_checker_can_approve(tmp_path: Path) -> None:
    sf = _sf(tmp_path)
    tid, maker, checker = _tenant_two_users(sf)
    svc = GovernanceService()
    with sf.session() as s:
        req = svc.open_gate(s, tenant_id=tid, action=_action(), requested_by=maker)
        s.commit()
        rid = req.id
        assert req.status == ApprovalStatus.PENDING

    with sf.session() as s:
        decided = svc.decide(
            s, tenant_id=tid, request_id=rid, approver_id=checker, approve=True,
            reason="looks good",
        )
        s.commit()
        assert decided.status == ApprovalStatus.APPROVED
        assert decided.decided_by == checker
        assert decided.reason == "looks good"


def test_maker_cannot_be_checker(tmp_path: Path) -> None:
    sf = _sf(tmp_path)
    tid, maker, _checker = _tenant_two_users(sf)
    svc = GovernanceService()
    with sf.session() as s:
        req = svc.open_gate(s, tenant_id=tid, action=_action(), requested_by=maker)
        s.commit()
        rid = req.id

    with sf.session() as s:
        with pytest.raises(SelfApprovalError):
            svc.decide(s, tenant_id=tid, request_id=rid, approver_id=maker, approve=True)
        # The request stays pending after a refused self-approval.
        s.rollback()


def test_cannot_decide_twice(tmp_path: Path) -> None:
    sf = _sf(tmp_path)
    tid, maker, checker = _tenant_two_users(sf)
    svc = GovernanceService()
    with sf.session() as s:
        rid = svc.open_gate(s, tenant_id=tid, action=_action(), requested_by=maker).id
        s.commit()
    with sf.session() as s:
        svc.decide(s, tenant_id=tid, request_id=rid, approver_id=checker, approve=False)
        s.commit()
    with sf.session() as s, pytest.raises(GovernanceError):
        svc.decide(s, tenant_id=tid, request_id=rid, approver_id=checker, approve=True)


def test_unknown_request_is_subject_gone(tmp_path: Path) -> None:
    sf = _sf(tmp_path)
    tid, _maker, checker = _tenant_two_users(sf)
    svc = GovernanceService()
    with sf.session() as s, pytest.raises(SubjectGoneError):
        svc.decide(
            s, tenant_id=tid, request_id=uuid.uuid4(), approver_id=checker,
            approve=True,
        )


def test_cross_tenant_request_is_invisible(tmp_path: Path) -> None:
    sf = _sf(tmp_path)
    tid, maker, _checker = _tenant_two_users(sf)
    svc = GovernanceService()
    with sf.session() as s:
        rid = svc.open_gate(s, tenant_id=tid, action=_action(), requested_by=maker).id
        s.commit()
    other_tenant = uuid.uuid4()
    with sf.session() as s, pytest.raises(SubjectGoneError):
        svc.decide(
            s, tenant_id=other_tenant, request_id=rid, approver_id=uuid.uuid4(),
            approve=True,
        )
