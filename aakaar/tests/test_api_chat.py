"""Chat → planner endpoint tests.

The planner uses a `FakeLLMClient` so these tests don't hit OpenAI. We
verify the three response shapes (dag/clarify/missing) round-trip cleanly,
and that chat enforces tenant grants via the underlying validator.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from pydantic import BaseModel

from aakaar.api.deps import AppDependencies
from aakaar.planner import FakeLLMClient, PlannerCompletion
from aakaar.shared.dag.types import Dag, Edge, Node, NodeKind
from aakaar.shared.registry import CapabilityDefinition
from tests._api_helpers import (
    auth_headers,
    login,
    seed_tenant_admin,
)


class _In(BaseModel):
    pass


class _Out(BaseModel):
    pass


def _dag_response_dag() -> Dag:
    return Dag(
        nodes=[
            Node(id="open", kind=NodeKind.ACTION, ref="browser.open_session"),
            Node(
                id="go",
                kind=NodeKind.ACTION,
                ref="browser.navigate",
                inputs={"session": "${open.session}", "url": "https://x"},
            ),
        ],
        edges=[Edge.model_validate({"from": "open", "to": "go"})],
    )


def test_chat_dag_response(
    deps: AppDependencies, fake_llm: FakeLLMClient, client: TestClient
) -> None:
    fake_llm.replies.append(
        PlannerCompletion(kind="dag", dag=_dag_response_dag(), rationale="opens and navigates")
    )
    seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="a@a.test", admin_password="adminpass1"
    )
    token = login(client, email="a@a.test", password="adminpass1")

    r = client.post(
        "/chat",
        headers=auth_headers(token),
        json={"message": "open a browser and go to x"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "dag"
    assert body["rationale"] == "opens and navigates"
    assert body["dag"]["nodes"][0]["ref"] == "browser.open_session"


def test_chat_clarify_response(
    deps: AppDependencies, fake_llm: FakeLLMClient, client: TestClient
) -> None:
    fake_llm.replies.append(
        PlannerCompletion(kind="clarify", questions=["which account?"])
    )
    seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="a@a.test", admin_password="adminpass1"
    )
    token = login(client, email="a@a.test", password="adminpass1")

    r = client.post(
        "/chat",
        headers=auth_headers(token),
        json={"message": "log in"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "clarify"
    assert body["questions"] == ["which account?"]


def test_chat_missing_response(
    deps: AppDependencies, fake_llm: FakeLLMClient, client: TestClient
) -> None:
    fake_llm.replies.append(
        PlannerCompletion(
            kind="missing",
            needed=["cap.icici_login"],
            explanation="ICICI capability not granted to this tenant.",
        )
    )
    seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="a@a.test", admin_password="adminpass1"
    )
    token = login(client, email="a@a.test", password="adminpass1")

    r = client.post(
        "/chat",
        headers=auth_headers(token),
        json={"message": "log into icici"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "missing"
    assert body["needed"] == ["cap.icici_login"]


def test_chat_rejects_dag_using_ungranted_capability(
    deps: AppDependencies, fake_llm: FakeLLMClient, client: TestClient
) -> None:
    deps.registry.add(
        CapabilityDefinition(
            ref="cap.requires_grant",
            description="needs grant",
            input_schema=_In,
            output_schema=_Out,
        )
    )
    bad_dag = Dag(
        nodes=[Node(id="login", kind=NodeKind.CAPABILITY, ref="cap.requires_grant")]
    )
    # Only ONE reply is needed now: an ungranted-only DAG short-circuits to a
    # `missing` result on the first attempt rather than exhausting the budget.
    fake_llm.replies.append(PlannerCompletion(kind="dag", dag=bad_dag))

    seed_tenant_admin(
        deps, slug="acme", name="Acme", admin_email="a@a.test", admin_password="adminpass1"
    )
    token = login(client, email="a@a.test", password="adminpass1")

    r = client.post(
        "/chat",
        headers=auth_headers(token),
        json={"message": "x"},
    )
    # The planner no longer 502s on an ungranted cap — it returns a helpful
    # `missing` response naming the capability that needs a grant.
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "missing"
    assert body["needed"] == ["cap.requires_grant"]
    assert "cap.requires_grant" in body["explanation"]
