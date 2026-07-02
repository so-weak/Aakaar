"""Tests for POST /workflows/preview — the deterministic plan-preview endpoint.

The endpoint is read-only and does NOT check grants: the UI calls it on the
current draft DAG (after chat returns it) to show what will happen and which
steps write or pause for a human, before offering Run.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from pydantic import BaseModel

from aakaar.api.deps import AppDependencies
from aakaar.shared.dag.types import Dag, Edge, Node, NodeKind
from aakaar.shared.registry import CapabilityDefinition
from tests._api_helpers import auth_headers, login, seed_tenant_admin


class _In(BaseModel):
    pass


class _Out(BaseModel):
    pass


def _edge(a: str, b: str) -> dict:
    return {"from": a, "to": b}


def _preview_body(dag: Dag) -> dict:
    return {"dag": dag.model_dump(by_alias=True)}


def _token(deps: AppDependencies, client: TestClient) -> str:
    seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="a@a.test", admin_password="adminpass1"
    )
    return login(client, email="a@a.test", password="adminpass1")


def test_preview_read_only_workflow(deps: AppDependencies, client: TestClient) -> None:
    token = _token(deps, client)
    dag = Dag(
        nodes=[
            Node(id="open", kind=NodeKind.ACTION, ref="browser.open_session"),
            Node(
                id="go",
                kind=NodeKind.ACTION,
                ref="browser.navigate",
                inputs={"session": "${open.session}", "url": "https://x"},
            ),
        ],
        edges=[Edge.model_validate(_edge("open", "go"))],
    )
    r = client.post("/workflows/preview", headers=auth_headers(token), json=_preview_body(dag))
    assert r.status_code == 200, r.text
    body = r.json()
    assert [s["node_id"] for s in body["steps"]] == ["open", "go"]
    assert body["highest_risk"] == "read"
    assert body["needs_confirmation"] is False
    assert body["requires_human"] is False
    assert body["risk_counts"]["read"] == 2


def test_preview_write_step_needs_confirmation(
    deps: AppDependencies, client: TestClient
) -> None:
    token = _token(deps, client)
    dag = Dag(
        nodes=[
            Node(id="open", kind=NodeKind.ACTION, ref="browser.open_session"),
            Node(
                id="set_amt",
                kind=NodeKind.ACTION,
                ref="browser.set_field",
                inputs={"session": "${open.session}", "label": "Amount", "value": "10"},
            ),
        ],
        edges=[Edge.model_validate(_edge("open", "set_amt"))],
    )
    r = client.post("/workflows/preview", headers=auth_headers(token), json=_preview_body(dag))
    assert r.status_code == 200, r.text
    body = r.json()
    risks = {s["node_id"]: s["risk"] for s in body["steps"]}
    assert risks["set_amt"] == "write"
    assert body["highest_risk"] == "write"
    assert body["needs_confirmation"] is True


def test_preview_high_risk_capability(deps: AppDependencies, client: TestClient) -> None:
    deps.registry.add(
        CapabilityDefinition(
            ref="cap.beneficiary_payment_transfer",
            description="Transfer money to a beneficiary.",
            input_schema=_In,
            output_schema=_Out,
            side_effecting=True,
            tags=("money",),
        )
    )
    token = _token(deps, client)
    dag = Dag(
        nodes=[
            Node(id="pay", kind=NodeKind.CAPABILITY, ref="cap.beneficiary_payment_transfer")
        ]
    )
    r = client.post("/workflows/preview", headers=auth_headers(token), json=_preview_body(dag))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["steps"][0]["risk"] == "high_risk"
    assert body["highest_risk"] == "high_risk"
    assert body["needs_confirmation"] is True


def test_preview_human_step_flagged(deps: AppDependencies, client: TestClient) -> None:
    token = _token(deps, client)
    dag = Dag(
        nodes=[
            Node(id="open", kind=NodeKind.ACTION, ref="browser.open_session"),
            Node(
                id="confirm",
                kind=NodeKind.CONTROL,
                ref="human.prompt",
                inputs={"message": "OK?", "expects": "confirm"},
            ),
        ],
        edges=[Edge.model_validate(_edge("open", "confirm"))],
    )
    r = client.post("/workflows/preview", headers=auth_headers(token), json=_preview_body(dag))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requires_human"] is True
    assert body["needs_confirmation"] is True
    confirm = next(s for s in body["steps"] if s["node_id"] == "confirm")
    assert confirm["requires_human"] is True


def test_preview_requires_auth(client: TestClient) -> None:
    dag = Dag(nodes=[Node(id="open", kind=NodeKind.ACTION, ref="browser.open_session")])
    r = client.post("/workflows/preview", json=_preview_body(dag))
    assert r.status_code == 401


def test_preview_does_not_collide_with_workflow_get(
    deps: AppDependencies, client: TestClient
) -> None:
    # /workflows/preview is a POST; the {workflow_id} routes are GET/PATCH/DELETE
    # — "preview" must never be coerced into a workflow_id lookup.
    token = _token(deps, client)
    dag = Dag(nodes=[Node(id="open", kind=NodeKind.ACTION, ref="browser.open_session")])
    r = client.post("/workflows/preview", headers=auth_headers(token), json=_preview_body(dag))
    assert r.status_code == 200, r.text
