"""Chat session endpoints — conversational planning + save/update flow.

Each test queues `FakeLLMClient` replies for the planner turns we expect,
then drives the session through the API and asserts the persisted state.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from aakaar.api.deps import AppDependencies
from aakaar.planner import FakeLLMClient, PlannerCompletion
from aakaar.planner.llm import ToolCall, ToolStep
from aakaar.shared.dag.types import Dag, Edge, Node, NodeKind
from tests._api_helpers import auth_headers, login, seed_tenant_admin


def _example_dag() -> Dag:
    return Dag(
        nodes=[
            Node(id="open", kind=NodeKind.ACTION, ref="browser.open_session"),
            Node(
                id="go",
                kind=NodeKind.ACTION,
                ref="browser.navigate",
                inputs={"session": "${open.session}", "url": "https://example.com"},
            ),
        ],
        edges=[Edge.model_validate({"from": "open", "to": "go"})],
    )


def _refined_dag() -> Dag:
    return Dag(
        nodes=[
            Node(id="open", kind=NodeKind.ACTION, ref="browser.open_session"),
            Node(
                id="go",
                kind=NodeKind.ACTION,
                ref="browser.navigate",
                inputs={"session": "${open.session}", "url": "https://example.com/refined"},
            ),
            Node(
                id="shot",
                kind=NodeKind.ACTION,
                ref="browser.screenshot",
                inputs={"session": "${open.session}"},
            ),
        ],
        edges=[
            Edge.model_validate({"from": "open", "to": "go"}),
            Edge.model_validate({"from": "go", "to": "shot"}),
        ],
    )


def _setup_user(deps: AppDependencies, client: TestClient) -> str:
    seed_tenant_admin(
        deps,
        slug="acme",
        name="Acme",
        admin_email="admin@acme.test",
        admin_password="adminpass1",
    )
    return login(client, email="admin@acme.test", password="adminpass1")


def test_session_lifecycle_create_send_save(
    deps: AppDependencies, fake_llm: FakeLLMClient, client: TestClient
) -> None:
    fake_llm.replies.append(
        PlannerCompletion(kind="dag", dag=_example_dag(), rationale="opens and navigates")
    )
    token = _setup_user(deps, client)

    # Create the session.
    r = client.post("/chat/sessions", headers=auth_headers(token), json={"title": "smoke"})
    assert r.status_code == 201, r.text
    sess = r.json()
    assert sess["title"] == "smoke"
    assert sess["draft_dag"] is None
    assert sess["is_dirty"] is False
    assert sess["messages"] == []
    sid = sess["id"]

    # Send a user message → planner reply lands on the session.
    r = client.post(
        f"/chat/sessions/{sid}/messages",
        headers=auth_headers(token),
        json={"message": "open example.com"},
    )
    assert r.status_code == 200, r.text
    sess = r.json()
    assert sess["draft_dag"] is not None
    assert sess["is_dirty"] is True  # draft, no save yet
    assert [m["role"] for m in sess["messages"]] == ["user", "planner"]
    assert sess["messages"][1]["payload"]["kind"] == "dag"

    # Save: requires name on first save.
    r = client.post(
        f"/chat/sessions/{sid}/save",
        headers=auth_headers(token),
        json={"name": "Example flow"},
    )
    assert r.status_code == 200, r.text
    workflow = r.json()
    assert workflow["name"] == "Example flow"
    assert workflow["latest_version"] == 1

    # Re-fetch the session → bound to the workflow, not dirty anymore.
    r = client.get(f"/chat/sessions/{sid}", headers=auth_headers(token))
    assert r.status_code == 200
    refreshed = r.json()
    assert refreshed["workflow_id"] == workflow["id"]
    assert refreshed["saved_version"] == 1
    assert refreshed["is_dirty"] is False


def test_session_save_first_time_requires_name(
    deps: AppDependencies, fake_llm: FakeLLMClient, client: TestClient
) -> None:
    fake_llm.replies.append(
        PlannerCompletion(kind="dag", dag=_example_dag(), rationale="ok")
    )
    token = _setup_user(deps, client)
    sid = client.post(
        "/chat/sessions", headers=auth_headers(token), json={}
    ).json()["id"]
    client.post(
        f"/chat/sessions/{sid}/messages",
        headers=auth_headers(token),
        json={"message": "open example.com"},
    )
    r = client.post(
        f"/chat/sessions/{sid}/save", headers=auth_headers(token), json={}
    )
    assert r.status_code == 400
    assert "name" in r.json()["detail"].lower()


def test_session_update_requires_confirm_when_dirty(
    deps: AppDependencies, fake_llm: FakeLLMClient, client: TestClient
) -> None:
    """First save creates. A second planner turn drifts the draft. Save
    without confirm → 409. With confirm → new version."""
    fake_llm.replies.extend(
        [
            PlannerCompletion(kind="dag", dag=_example_dag(), rationale="v1"),
            PlannerCompletion(kind="dag", dag=_refined_dag(), rationale="v2"),
        ]
    )
    token = _setup_user(deps, client)

    sid = client.post(
        "/chat/sessions", headers=auth_headers(token), json={"title": "iter"}
    ).json()["id"]

    # Turn 1 + save.
    client.post(
        f"/chat/sessions/{sid}/messages",
        headers=auth_headers(token),
        json={"message": "draft v1"},
    )
    r = client.post(
        f"/chat/sessions/{sid}/save",
        headers=auth_headers(token),
        json={"name": "Iter flow"},
    )
    assert r.status_code == 200
    wf = r.json()
    assert wf["latest_version"] == 1

    # Turn 2 — drifts the draft.
    r = client.post(
        f"/chat/sessions/{sid}/messages",
        headers=auth_headers(token),
        json={"message": "also screenshot it"},
    )
    assert r.json()["is_dirty"] is True

    # Save without confirm → 409.
    r = client.post(
        f"/chat/sessions/{sid}/save", headers=auth_headers(token), json={}
    )
    assert r.status_code == 409, r.text
    assert "confirm" in r.json()["detail"].lower()

    # Save with confirm → new version.
    r = client.post(
        f"/chat/sessions/{sid}/save",
        headers=auth_headers(token),
        json={"confirm": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["latest_version"] == 2

    # No drift now.
    r = client.get(f"/chat/sessions/{sid}", headers=auth_headers(token))
    assert r.json()["is_dirty"] is False


def test_session_save_clean_is_idempotent(
    deps: AppDependencies, fake_llm: FakeLLMClient, client: TestClient
) -> None:
    """Saving a clean (unchanged) session returns the existing workflow
    without writing a new version."""
    fake_llm.replies.append(
        PlannerCompletion(kind="dag", dag=_example_dag(), rationale="ok")
    )
    token = _setup_user(deps, client)
    sid = client.post("/chat/sessions", headers=auth_headers(token), json={}).json()["id"]
    client.post(
        f"/chat/sessions/{sid}/messages",
        headers=auth_headers(token),
        json={"message": "draft"},
    )
    r1 = client.post(
        f"/chat/sessions/{sid}/save",
        headers=auth_headers(token),
        json={"name": "wf"},
    )
    assert r1.status_code == 200
    # Save again with no changes — same version, no write needed.
    r2 = client.post(
        f"/chat/sessions/{sid}/save",
        headers=auth_headers(token),
        json={"confirm": True},
    )
    assert r2.status_code == 200
    assert r2.json()["latest_version"] == 1


def test_session_chat_history_threaded_to_planner(
    deps: AppDependencies, fake_llm: FakeLLMClient, client: TestClient
) -> None:
    """Two-turn refinement: assert the planner's second call saw the
    user's first turn and the planner's first reply."""
    fake_llm.replies.extend(
        [
            PlannerCompletion(kind="clarify", questions=["What URL?"]),
            PlannerCompletion(kind="dag", dag=_example_dag(), rationale="thanks"),
        ]
    )
    token = _setup_user(deps, client)
    sid = client.post("/chat/sessions", headers=auth_headers(token), json={}).json()["id"]
    client.post(
        f"/chat/sessions/{sid}/messages",
        headers=auth_headers(token),
        json={"message": "open something for me"},
    )
    client.post(
        f"/chat/sessions/{sid}/messages",
        headers=auth_headers(token),
        json={"message": "https://example.com"},
    )

    # The fake recorded both calls; the second should include the prior
    # user turn and the planner's clarify reply as conversation context.
    assert len(fake_llm.calls) == 2
    second = fake_llm.calls[1]
    roles = [m.role.value for m in second]
    # system + (user, assistant from history) + (current user) at minimum
    assert "system" in roles
    assert roles.count("user") >= 2
    assert "assistant" in roles


def test_session_isolation_between_users(
    deps: AppDependencies, client: TestClient
) -> None:
    """A session belongs to its creator. A different user in the same
    tenant cannot access it."""
    from tests._api_helpers import seed_tenant_user

    tenant, _admin = seed_tenant_admin(
        deps,
        slug="iso",
        name="Iso",
        admin_email="a1@iso.test",
        admin_password="adminpass1",
    )
    seed_tenant_user(
        deps,
        tenant_id=tenant.id,
        email="b@iso.test",
        password="userpass1",
    )
    token_a = login(client, email="a1@iso.test", password="adminpass1")
    token_b = login(client, email="b@iso.test", password="userpass1")
    sid = client.post(
        "/chat/sessions", headers=auth_headers(token_a), json={"title": "private"}
    ).json()["id"]

    r = client.get(f"/chat/sessions/{sid}", headers=auth_headers(token_b))
    assert r.status_code == 404


def test_session_404_for_unknown_id(
    deps: AppDependencies, client: TestClient
) -> None:
    token = _setup_user(deps, client)
    r = client.get(
        "/chat/sessions/00000000-0000-0000-0000-000000000000",
        headers=auth_headers(token),
    )
    assert r.status_code == 404


def test_clarify_auto_falls_back_to_agentic(
    deps: AppDependencies, fake_llm: FakeLLMClient, client: TestClient
) -> None:
    """If the one-shot planner returns clarify and an agentic planner is
    wired (the test fixture supplies a FakeBrowserPool, so it is), the
    chat session endpoint retries agentically and the agentic DAG
    response replaces the original clarify. The frontend never sees the
    clarify; it sees the DAG."""
    # First reply (one-shot): clarify — would normally stop here.
    fake_llm.replies.append(
        PlannerCompletion(kind="clarify", questions=["Which page?"])
    )
    # Agentic loop: navigate → done(dag).
    fake_llm.tool_steps.extend(
        [
            ToolStep(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="navigate",
                        arguments={"url": "https://example.com"},
                    )
                ],
                final_content=None,
            ),
            ToolStep(
                tool_calls=[
                    ToolCall(
                        id="c2",
                        name="done",
                        arguments={
                            "kind": "dag",
                            "rationale": "auto-fallback figured it out",
                            "dag": _example_dag().model_dump(by_alias=True),
                        },
                    )
                ],
                final_content=None,
            ),
        ]
    )

    token = _setup_user(deps, client)
    sid = client.post("/chat/sessions", headers=auth_headers(token), json={}).json()["id"]
    r = client.post(
        f"/chat/sessions/{sid}/messages",
        headers=auth_headers(token),
        json={"message": "open something"},
    )
    assert r.status_code == 200, r.text
    sess = r.json()

    # The persisted planner reply is the agentic DAG, not the one-shot clarify.
    planner_msgs = [m for m in sess["messages"] if m["role"] == "planner"]
    assert len(planner_msgs) == 1
    assert planner_msgs[0]["payload"]["kind"] == "dag"
    assert planner_msgs[0]["payload"]["rationale"] == "auto-fallback figured it out"
    assert sess["draft_dag"] is not None
    # And the agentic loop actually ran.
    assert fake_llm.tool_call_history, "agentic LLM was never invoked"


def test_clarify_kept_when_agentic_also_clarifies(
    deps: AppDependencies, fake_llm: FakeLLMClient, client: TestClient
) -> None:
    """If both planners ask for clarification, keep the one-shot's
    questions — they're usually crisper than the 'I explored and gave
    up' agentic fallback."""
    fake_llm.replies.append(
        PlannerCompletion(kind="clarify", questions=["What is the URL?"])
    )
    fake_llm.tool_steps.append(
        ToolStep(
            tool_calls=[],  # agentic gives up immediately
            final_content="I need more info too",
        )
    )

    token = _setup_user(deps, client)
    sid = client.post("/chat/sessions", headers=auth_headers(token), json={}).json()["id"]
    r = client.post(
        f"/chat/sessions/{sid}/messages",
        headers=auth_headers(token),
        json={"message": "do something"},
    )
    assert r.status_code == 200
    sess = r.json()
    planner_msgs = [m for m in sess["messages"] if m["role"] == "planner"]
    assert planner_msgs[-1]["payload"]["kind"] == "clarify"
    assert planner_msgs[-1]["payload"]["questions"] == ["What is the URL?"]


# ---------- W1: direct draft editing (PUT /draft) ---------------------------


def test_update_draft_replaces_dag(
    deps: AppDependencies, fake_llm: FakeLLMClient, client: TestClient
) -> None:
    """PUT /draft overwrites the session's draft with a hand-edited DAG and
    the refreshed session reflects the new graph."""
    fake_llm.replies.append(
        PlannerCompletion(kind="dag", dag=_example_dag(), rationale="v1")
    )
    token = _setup_user(deps, client)
    sid = client.post("/chat/sessions", headers=auth_headers(token), json={}).json()["id"]
    client.post(
        f"/chat/sessions/{sid}/messages",
        headers=auth_headers(token),
        json={"message": "draft it"},
    )

    r = client.put(
        f"/chat/sessions/{sid}/draft",
        headers=auth_headers(token),
        json=_refined_dag().model_dump(by_alias=True),
    )
    assert r.status_code == 200, r.text
    sess = r.json()
    node_ids = [n["id"] for n in sess["draft_dag"]["nodes"]]
    assert node_ids == ["open", "go", "shot"]  # the refined graph won
    assert sess["is_dirty"] is True


def test_update_draft_rejects_malformed(
    deps: AppDependencies, client: TestClient
) -> None:
    """A body that isn't a valid DAG is rejected at the boundary (422),
    so a corrupt draft can never be persisted."""
    token = _setup_user(deps, client)
    sid = client.post("/chat/sessions", headers=auth_headers(token), json={}).json()["id"]
    r = client.put(
        f"/chat/sessions/{sid}/draft",
        headers=auth_headers(token),
        json={"nodes": "not-a-list"},
    )
    assert r.status_code == 422, r.text


def test_update_draft_owner_scoped(deps: AppDependencies, client: TestClient) -> None:
    """A different user in the same tenant cannot edit someone's draft."""
    from tests._api_helpers import seed_tenant_user

    tenant, _admin = seed_tenant_admin(
        deps,
        slug="draft",
        name="Draft",
        admin_email="a@draft.test",
        admin_password="adminpass1",
    )
    seed_tenant_user(
        deps, tenant_id=tenant.id, email="b@draft.test", password="userpass1"
    )
    token_a = login(client, email="a@draft.test", password="adminpass1")
    token_b = login(client, email="b@draft.test", password="userpass1")
    sid = client.post(
        "/chat/sessions", headers=auth_headers(token_a), json={}
    ).json()["id"]

    r = client.put(
        f"/chat/sessions/{sid}/draft",
        headers=auth_headers(token_b),
        json=_example_dag().model_dump(by_alias=True),
    )
    assert r.status_code == 404


# ---------- W2: workflow-refine seeding -------------------------------------


def _save_workflow_via_chat(
    client: TestClient, token: str, fake_llm: FakeLLMClient, *, name: str, seed: str
) -> str:
    """Create a chat session, draft a DAG, and save it as a workflow.
    Returns the workflow id. Queues one planner reply."""
    fake_llm.replies.append(
        PlannerCompletion(kind="dag", dag=_example_dag(), rationale="drafted")
    )
    sid = client.post("/chat/sessions", headers=auth_headers(token), json={}).json()["id"]
    client.post(
        f"/chat/sessions/{sid}/messages",
        headers=auth_headers(token),
        json={"message": seed},
    )
    wf = client.post(
        f"/chat/sessions/{sid}/save",
        headers=auth_headers(token),
        json={"name": name},
    ).json()
    return wf["id"]


def test_refine_create_seeds_from_workflow(
    deps: AppDependencies, fake_llm: FakeLLMClient, client: TestClient
) -> None:
    """Creating a session with workflow_id opens a 'Refine:' session bound
    to the workflow, pre-loaded with its latest draft and a composer seed."""
    token = _setup_user(deps, client)
    wf_id = _save_workflow_via_chat(
        client, token, fake_llm, name="Payroll", seed="open the payroll page"
    )

    r = client.post(
        "/chat/sessions",
        headers=auth_headers(token),
        json={"workflow_id": wf_id},
    )
    assert r.status_code == 201, r.text
    sess = r.json()
    assert sess["title"] == "Refine: Payroll"
    assert sess["workflow_id"] == wf_id
    assert sess["saved_version"] == 1
    assert sess["draft_dag"] is not None
    assert sess["is_dirty"] is False  # matches the saved version
    # Composer is seeded with the user's most recent instruction.
    assert sess["composer_seed"] == "open the payroll page"


def test_refine_create_rejects_non_owner(
    deps: AppDependencies, fake_llm: FakeLLMClient, client: TestClient
) -> None:
    """Only the workflow's owner can open a refine session for it."""
    from tests._api_helpers import seed_tenant_user

    tenant, _admin = seed_tenant_admin(
        deps,
        slug="refine",
        name="Refine",
        admin_email="a@refine.test",
        admin_password="adminpass1",
    )
    seed_tenant_user(
        deps, tenant_id=tenant.id, email="b@refine.test", password="userpass1"
    )
    token_a = login(client, email="a@refine.test", password="adminpass1")
    token_b = login(client, email="b@refine.test", password="userpass1")
    wf_id = _save_workflow_via_chat(
        client, token_a, fake_llm, name="Owned", seed="do a thing"
    )

    r = client.post(
        "/chat/sessions",
        headers=auth_headers(token_b),
        json={"workflow_id": wf_id},
    )
    assert r.status_code == 403, r.text
