"""End-to-end maker-checker gate: a gated run-start is held as a pending
approval, the maker cannot approve their own request (segregation of duties),
and a different admin's approval actually starts the run."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from aakaar.api.auth import hash_password
from aakaar.api.deps import AppDependencies
from aakaar.db.models import Run, User, UserRole, UserStatus
from aakaar.shared.dag.types import Dag, Node, NodeKind
from tests._api_helpers import auth_headers, login, seed_tenant_admin


def _gated_workflow(client: TestClient, token: str) -> str:
    dag = Dag(
        nodes=[
            Node(
                id="approve",
                kind=NodeKind.CONTROL,
                ref="human.prompt",
                inputs={"message": "release funds?", "timeout_seconds": 30},
            )
        ]
    )
    r = client.post(
        "/workflows",
        headers=auth_headers(token),
        json={
            "name": "wire-transfer",
            "description": "",
            "dag": dag.model_dump(by_alias=True),
            "requires_approval": True,
            "sensitivity": "elevated",
        },
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def test_gated_run_requires_a_second_admin(
    client: TestClient, deps: AppDependencies
) -> None:
    tenant, _maker = seed_tenant_admin(
        deps,
        slug="bank",
        name="Bank",
        admin_email="maker@bank.test",
        admin_password="pw-maker-123456",
    )
    # A second admin in the same tenant — the checker.
    with deps.session_factory.session() as s:
        s.add(
            User(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                email="checker@bank.test",
                password_hash=hash_password("pw-check-123456"),
                role=UserRole.TENANT_ADMIN,
                status=UserStatus.ACTIVE,
            )
        )
        s.commit()

    maker = login(client, email="maker@bank.test", password="pw-maker-123456")
    checker = login(client, email="checker@bank.test", password="pw-check-123456")

    wf_id = _gated_workflow(client, maker)

    # Maker starts a run -> NOT launched; held as a pending approval (202).
    r = client.post(
        f"/workflows/{wf_id}/runs", headers=auth_headers(maker), json={"inputs": {}}
    )
    assert r.status_code == 202, r.text
    approval_id = r.json()["approval"]["id"]
    # No run exists yet — the gate held it.
    with deps.session_factory.session() as s:
        assert list(s.scalars(select(Run).where(Run.workflow_id == uuid.UUID(wf_id)))) == []

    # Segregation of duties: the maker cannot approve their own request.
    r_self = client.post(
        f"/approvals/{approval_id}/approve",
        headers=auth_headers(maker),
        json={"reason": "self"},
    )
    assert r_self.status_code == 409, r_self.text

    # A different admin approves -> the run is now actually created.
    r_ok = client.post(
        f"/approvals/{approval_id}/approve",
        headers=auth_headers(checker),
        json={"reason": "verified counterparty"},
    )
    assert r_ok.status_code == 200, r_ok.text
    assert r_ok.json()["status"] == "approved"

    with deps.session_factory.session() as s:
        runs = list(s.scalars(select(Run).where(Run.workflow_id == uuid.UUID(wf_id))))
    assert len(runs) == 1  # the approval performed the gated run-start


def test_gated_run_can_be_rejected(client: TestClient, deps: AppDependencies) -> None:
    tenant, _maker = seed_tenant_admin(
        deps,
        slug="bank2",
        name="Bank2",
        admin_email="maker2@bank.test",
        admin_password="pw-maker-123456",
    )
    with deps.session_factory.session() as s:
        s.add(
            User(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                email="checker2@bank.test",
                password_hash=hash_password("pw-check-123456"),
                role=UserRole.TENANT_ADMIN,
                status=UserStatus.ACTIVE,
            )
        )
        s.commit()
    maker = login(client, email="maker2@bank.test", password="pw-maker-123456")
    checker = login(client, email="checker2@bank.test", password="pw-check-123456")
    wf_id = _gated_workflow(client, maker)

    approval_id = client.post(
        f"/workflows/{wf_id}/runs", headers=auth_headers(maker), json={"inputs": {}}
    ).json()["approval"]["id"]
    r = client.post(
        f"/approvals/{approval_id}/reject",
        headers=auth_headers(checker),
        json={"reason": "counterparty unverified"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"
    # Rejection performs nothing — no run is created.
    with deps.session_factory.session() as s:
        assert list(s.scalars(select(Run).where(Run.workflow_id == uuid.UUID(wf_id)))) == []
