"""Regression guards for the rerun endpoint's divergence from the reference fork.

Finding 2 flagged the rerun endpoint as near-verbatim with a diverged sibling
fork. The remediation reworded the 409 detail strings and restructured the
audit payload to this repo's own shape. These tests pin that shape so it can't
silently regress back toward the reference (flat {source_run_id, workflow_id,
version} keys / "only completed, failed, or cancelled runs can be re-run").

They also document the deliberately-dropped behavior: the launch-time run
target is not persisted on the Run row, so a rerun does not restore it.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from aakaar.api.deps import AppDependencies
from aakaar.db.models import AuditLog
from aakaar.shared.dag.types import Dag, Node, NodeKind
from tests._api_helpers import auth_headers, login, seed_tenant_admin, seed_tenant_user


def _seed(deps: AppDependencies, client: TestClient, slug: str = "acme") -> str:
    tenant, _ = seed_tenant_admin(
        deps,
        slug=slug,
        name=slug.title(),
        admin_email=f"a@{slug}.test",
        admin_password="adminpass1",
    )
    seed_tenant_user(deps, tenant_id=tenant.id, email=f"u@{slug}.test", password="userpass1")
    return login(client, email=f"u@{slug}.test", password="userpass1")


def _fast_dag() -> Dag:
    return Dag(
        nodes=[
            Node(id="n1", kind=NodeKind.CONTROL, ref="control.wait", inputs={"seconds": 0.01})
        ]
    )


def _save_workflow(client: TestClient, token: str, dag: Dag) -> str:
    r = client.post(
        "/workflows",
        headers=auth_headers(token),
        json={"name": "demo", "description": "", "dag": dag.model_dump(by_alias=True)},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _start(client: TestClient, token: str, wf_id: str, **body) -> str:
    r = client.post(f"/workflows/{wf_id}/runs", headers=auth_headers(token), json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _wait_terminal(client: TestClient, token: str, run_id: str) -> dict:
    import time

    for _ in range(100):
        body = client.get(f"/runs/{run_id}", headers=auth_headers(token)).json()
        if body["run"]["status"] in {"succeeded", "failed", "cancelled"}:
            return body
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} never reached terminal; last={body}")


def test_rerun_audit_payload_uses_this_repos_shape(
    deps: AppDependencies, client: TestClient
) -> None:
    """The rerun audit payload must use the nested {rerun_of, workflow:{id,
    version}, node_count} shape, NOT the reference fork's flat keys."""
    token = _seed(deps, client)
    wf_id = _save_workflow(client, token, _fast_dag())
    source_id = _start(client, token, wf_id, inputs={"k": "v"})
    _wait_terminal(client, token, source_id)

    r = client.post(f"/runs/{source_id}/rerun", headers=auth_headers(token))
    assert r.status_code == 201, r.text
    rerun_id = r.json()["id"]

    with deps.session_factory.session() as s:
        row = s.scalars(
            select(AuditLog).where(
                AuditLog.action == "run.rerun", AuditLog.target_id == rerun_id
            )
        ).one()
    payload = row.payload
    assert payload["rerun_of"] == source_id
    assert payload["workflow"] == {
        "id": str(uuid.UUID(wf_id)),
        "version": 1,
    }
    assert payload["node_count"] == 1
    # The reference fork's flat keys must NOT be present.
    assert "source_run_id" not in payload
    assert "workflow_id" not in payload
    assert "version" not in payload


def test_rerun_active_run_409_message_is_reworded(
    deps: AppDependencies, client: TestClient
) -> None:
    """The active-run 409 detail must be this repo's wording, not the
    reference's 'only completed, failed, or cancelled runs can be re-run'."""
    token = _seed(deps, client)
    # A human.prompt run stays active until answered.
    wf_id = _save_workflow(
        client,
        token,
        Dag(
            nodes=[
                Node(
                    id="ask",
                    kind=NodeKind.CONTROL,
                    ref="human.prompt",
                    inputs={"message": "?", "timeout_seconds": 30},
                )
            ]
        ),
    )
    run_id = _start(client, token, wf_id)
    # Wait for the prompt to register so the run is unambiguously active.
    import time

    for _ in range(100):
        body = client.get(f"/runs/{run_id}", headers=auth_headers(token)).json()
        if body["pending_prompts"]:
            break
        time.sleep(0.05)

    r = client.post(f"/runs/{run_id}/rerun", headers=auth_headers(token))
    assert r.status_code == 409
    assert r.json()["detail"] == "run is still active; rerun is only allowed once it has finished"

    client.post(
        f"/runs/{run_id}/respond",
        headers=auth_headers(token),
        json={"node_id": "ask", "response": "ok"},
    )
    _wait_terminal(client, token, run_id)


def test_rerun_does_not_restore_launch_target(
    deps: AppDependencies, client: TestClient
) -> None:
    """Documented gap: the launch-time run target is not persisted, so a rerun
    starts unpinned (target=None) regardless of the source run's launch target.
    A "server" launch target still reruns fine — it just isn't carried over."""
    token = _seed(deps, client)
    wf_id = _save_workflow(client, token, _fast_dag())
    source_id = _start(client, token, wf_id, target="server")
    _wait_terminal(client, token, source_id)

    r = client.post(f"/runs/{source_id}/rerun", headers=auth_headers(token))
    assert r.status_code == 201, r.text
    body = _wait_terminal(client, token, r.json()["id"])
    assert body["run"]["status"] == "succeeded"
