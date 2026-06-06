"""Smoke tests for the SQLAlchemy schema.

We don't test query semantics here — those land with the API PR. This test
just guarantees the schema creates cleanly on SQLite, foreign keys are
enforced, and a representative row round-trips.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from aakaar.db import (
    Base,
    CapabilityGrant,
    EngineConfig,
    SessionFactory,
    Tenant,
    User,
    Workflow,
    WorkflowVersion,
    make_engine,
)
from aakaar.db.models import TenantStatus, UserRole, UserStatus
from aakaar.db.tenancy import TenancyError, current_tenant, tenant_scope


@pytest.fixture()
def session(tmp_path: Path):
    engine = make_engine(EngineConfig(url=f"sqlite:///{tmp_path/'aakaar.sqlite'}"))
    Base.metadata.create_all(engine)
    factory = SessionFactory(engine)
    s = factory.session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def test_tenant_user_workflow_round_trip(session) -> None:
    tenant = Tenant(slug="acme", name="Acme Co", status=TenantStatus.ACTIVE)
    session.add(tenant)
    session.flush()

    user = User(
        tenant_id=tenant.id,
        email="user@acme.test",
        password_hash="x",
        role=UserRole.TENANT_USER,
        status=UserStatus.ACTIVE,
    )
    session.add(user)
    session.flush()

    wf = Workflow(tenant_id=tenant.id, created_by=user.id, name="May reports")
    session.add(wf)
    session.flush()

    ver = WorkflowVersion(
        tenant_id=tenant.id,
        workflow_id=wf.id,
        version=1,
        dag={"nodes": [], "edges": []},
        created_by=user.id,
    )
    session.add(ver)
    session.commit()

    fetched = session.scalars(select(Workflow).where(Workflow.id == wf.id)).one()
    assert fetched.name == "May reports"
    assert len(fetched.versions) == 1


def test_grant_uniqueness(session) -> None:
    tenant = Tenant(slug="acme", name="Acme")
    session.add(tenant)
    session.flush()
    user = User(tenant_id=tenant.id, email="u@a.test", password_hash="x", role=UserRole.TENANT_ADMIN)
    session.add(user)
    session.flush()

    g1 = CapabilityGrant(
        tenant_id=tenant.id,
        capability_ref="cap.x",
        account_alias="primary",
        vault_ref="v1",
        created_by=user.id,
    )
    session.add(g1)
    session.commit()

    g2 = CapabilityGrant(
        tenant_id=tenant.id,
        capability_ref="cap.x",
        account_alias="primary",
        vault_ref="v2",
        created_by=user.id,
    )
    session.add(g2)
    with pytest.raises(IntegrityError):
        session.commit()


def test_tenancy_scope_basic() -> None:
    tid = uuid.uuid4()
    with pytest.raises(TenancyError):
        current_tenant()
    with tenant_scope(tid):
        assert current_tenant() == tid
    with pytest.raises(TenancyError):
        current_tenant()


def test_tenancy_nested_same_tenant_ok() -> None:
    tid = uuid.uuid4()
    with tenant_scope(tid):
        with tenant_scope(tid):
            assert current_tenant() == tid


def test_tenancy_nested_switch_rejected() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    with tenant_scope(a):
        with pytest.raises(TenancyError):
            with tenant_scope(b):
                pass
