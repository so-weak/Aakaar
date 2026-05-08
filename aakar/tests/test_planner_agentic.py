"""Agentic planner tests.

A scripted `FakeLLMClient.tool_steps` queue stands in for the real LLM —
each step represents one round of the loop (tool calls or final
content). We drive the loop end-to-end against a `FakeBrowserPool` and
assert the dispatched tool calls + resulting `PlannerResponse`.

Covers:
  - happy path: navigate → inspect_page → done(dag) produces a valid DAG
  - login_with_grant uses the vault, refuses missing grants
  - iteration cap → clarify with observation list
  - LLM emits final_content (gives up on tools) → clarify
  - done(kind='clarify') passes through
  - DAG validation failure on done → clarify with the validator's complaint
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from aakar.capabilities import load_into
from aakar.capabilities.web_login import CAP_REF as WEB_LOGIN_REF
from aakar.planner.agentic.service import AgenticPlannerService
from aakar.planner.llm import FakeLLMClient, ToolCall, ToolStep
from aakar.shared.dag.types import Dag, Edge, Node, NodeKind
from aakar.shared.planner.responses import (
    ClarifyResponse,
    DagResponse,
    MissingResponse,
)
from aakar.shared.registry import build_default_registry
from aakar.vault import LocalVault
from aakar.workers.browser import FakeBrowserPool, FakeBrowserSession
from tests._discovery_helpers import discovery_response


def _good_dag() -> dict:
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
    ).model_dump(by_alias=True)


def _registry_with_caps():
    reg = build_default_registry()
    # We don't need handlers for plan-time validation, but `load_into` is
    # cheap and keeps cap.* refs visible to the validator.
    from aakar.interpreter import build_default_activities

    load_into(reg, build_default_activities())
    return reg


def _service(
    *,
    llm: FakeLLMClient,
    pool: FakeBrowserPool,
    vault: LocalVault | None = None,
    max_tool_calls: int = 12,
    deadline_seconds: float = 60.0,
    registry=None,
) -> AgenticPlannerService:
    return AgenticPlannerService(
        registry=registry or _registry_with_caps(),
        llm=llm,
        browser_pool=pool,
        vault=vault or LocalVault(Path("/tmp") / f"vault-{uuid.uuid4().hex}"),
        max_tool_calls=max_tool_calls,
        deadline_seconds=deadline_seconds,
    )


@pytest.mark.asyncio
async def test_happy_path_navigate_inspect_done_emits_dag(tmp_path: Path) -> None:
    """A normal three-call loop: navigate → inspect_page → done(dag)."""
    sess = FakeBrowserSession(
        evaluate_responses={
            # The runner's inspect JS calls into Playwright's evaluate()
            # with our own marker — the FakeBrowserSession returns
            # whatever was preloaded by inspect-relevant substring.
            "interactive": {
                "url": "https://example.com",
                "title": "Example",
                "visible_text": "hello",
                "interactive": [],
                "interactive_truncated": False,
                "interactive_count_total": 0,
            },
        }
    )
    pool = FakeBrowserPool(next_sessions=[sess])

    llm = FakeLLMClient()
    llm.tool_steps.extend(
        [
            ToolStep(
                tool_calls=[
                    ToolCall(id="c1", name="navigate", arguments={"url": "https://example.com"}),
                ],
                final_content=None,
            ),
            ToolStep(
                tool_calls=[ToolCall(id="c2", name="inspect_page", arguments={})],
                final_content=None,
            ),
            ToolStep(
                tool_calls=[
                    ToolCall(
                        id="c3",
                        name="done",
                        arguments={
                            "kind": "dag",
                            "rationale": "navigate then done",
                            "dag": _good_dag(),
                        },
                    ),
                ],
                final_content=None,
            ),
        ]
    )

    service = _service(llm=llm, pool=pool, vault=LocalVault(tmp_path / "vault"))
    resp = await service.plan(
        user_message="Open example.com",
        tenant_id=uuid.uuid4(),
        granted_capabilities=set(),
        granted_capability_grants={},
    )
    assert isinstance(resp, DagResponse)
    assert resp.rationale == "navigate then done"
    assert len(resp.dag.nodes) == 2

    # Verify the runner actually drove the browser in the right order.
    kinds = [c[0] for c in sess.calls]
    assert kinds[:1] == ["navigate"]
    assert "evaluate" in kinds
    assert sess.closed


@pytest.mark.asyncio
async def test_iteration_cap_returns_clarify(tmp_path: Path) -> None:
    """Loop never sees done; we hit the cap and return clarify."""
    sess = FakeBrowserSession(
        evaluate_responses={
            "interactive": {
                "url": "x",
                "title": "x",
                "visible_text": "",
                "interactive": [],
                "interactive_truncated": False,
                "interactive_count_total": 0,
            }
        }
    )
    pool = FakeBrowserPool(next_sessions=[sess])
    llm = FakeLLMClient()
    # Fewer steps queued than the cap means the queue exhausts. To
    # *test* the cap, we'd need exactly cap+1 steps that never call done.
    # Set max_tool_calls=2 and queue 2 inspect calls, ensuring no done.
    llm.tool_steps.extend(
        [
            ToolStep(
                tool_calls=[ToolCall(id=f"c{i}", name="inspect_page", arguments={})],
                final_content=None,
            )
            for i in range(2)
        ]
    )

    service = _service(
        llm=llm, pool=pool, vault=LocalVault(tmp_path / "vault"), max_tool_calls=2
    )
    resp = await service.plan(
        user_message="figure it out",
        tenant_id=uuid.uuid4(),
        granted_capabilities=set(),
        granted_capability_grants={},
    )
    assert isinstance(resp, ClarifyResponse)
    # The clarify should mention either iteration cap or include observation
    # summaries we accrued along the way.
    blob = " ".join(resp.questions).lower()
    assert "couldn't finalize" in blob or "iteration cap" in blob


@pytest.mark.asyncio
async def test_llm_gives_up_on_tools_returns_clarify(tmp_path: Path) -> None:
    """When the LLM emits final_content with no tool calls, we surface
    that as a clarify instead of looping forever."""
    pool = FakeBrowserPool(next_sessions=[FakeBrowserSession()])
    llm = FakeLLMClient()
    llm.tool_steps.append(
        ToolStep(
            tool_calls=[],
            final_content="I need the URL of the site you want me to log in to.",
        )
    )
    service = _service(llm=llm, pool=pool, vault=LocalVault(tmp_path / "vault"))
    resp = await service.plan(
        user_message="log me in",
        tenant_id=uuid.uuid4(),
        granted_capabilities=set(),
        granted_capability_grants={},
    )
    assert isinstance(resp, ClarifyResponse)
    assert any("URL" in q for q in resp.questions)


@pytest.mark.asyncio
async def test_done_kind_clarify_passes_through(tmp_path: Path) -> None:
    pool = FakeBrowserPool(next_sessions=[FakeBrowserSession()])
    llm = FakeLLMClient()
    llm.tool_steps.append(
        ToolStep(
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="done",
                    arguments={
                        "kind": "clarify",
                        "rationale": "ambiguous",
                        "questions": ["Which site?"],
                    },
                )
            ],
            final_content=None,
        )
    )
    service = _service(llm=llm, pool=pool, vault=LocalVault(tmp_path / "vault"))
    resp = await service.plan(
        user_message="do something",
        tenant_id=uuid.uuid4(),
        granted_capabilities=set(),
        granted_capability_grants={},
    )
    assert isinstance(resp, ClarifyResponse)
    assert resp.questions == ["Which site?"]


@pytest.mark.asyncio
async def test_done_kind_missing_passes_through(tmp_path: Path) -> None:
    pool = FakeBrowserPool(next_sessions=[FakeBrowserSession()])
    llm = FakeLLMClient()
    llm.tool_steps.append(
        ToolStep(
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="done",
                    arguments={
                        "kind": "missing",
                        "rationale": "no grant",
                        "needed": ["cap.salesforce_login"],
                        "explanation": "set up a Salesforce grant",
                    },
                )
            ],
            final_content=None,
        )
    )
    service = _service(llm=llm, pool=pool, vault=LocalVault(tmp_path / "vault"))
    resp = await service.plan(
        user_message="do salesforce",
        tenant_id=uuid.uuid4(),
        granted_capabilities=set(),
        granted_capability_grants={},
    )
    assert isinstance(resp, MissingResponse)
    assert resp.needed == ["cap.salesforce_login"]


@pytest.mark.asyncio
async def test_done_with_invalid_dag_returns_clarify(tmp_path: Path) -> None:
    """If the LLM emits a DAG referencing an ungranted capability, the
    validator complains and we return clarify (not propagate the
    PlannerError up — agentic mode should self-recover)."""
    pool = FakeBrowserPool(next_sessions=[FakeBrowserSession()])
    bad_dag = Dag(
        nodes=[
            Node(
                id="login",
                kind=NodeKind.CAPABILITY,
                ref=WEB_LOGIN_REF,
                inputs={"account_alias": "primary", "login_url": "https://x"},
            )
        ]
    ).model_dump(by_alias=True)
    llm = FakeLLMClient()
    llm.tool_steps.append(
        ToolStep(
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="done",
                    arguments={"kind": "dag", "rationale": "do it", "dag": bad_dag},
                )
            ],
            final_content=None,
        )
    )
    service = _service(llm=llm, pool=pool, vault=LocalVault(tmp_path / "vault"))
    resp = await service.plan(
        user_message="log in",
        tenant_id=uuid.uuid4(),
        granted_capabilities=set(),  # cap.web_login deliberately NOT granted
        granted_capability_grants={},
    )
    assert isinstance(resp, ClarifyResponse)
    assert any("validate" in q.lower() or "ungranted" in q.lower() for q in resp.questions)


@pytest.mark.asyncio
async def test_login_with_grant_fetches_vault_creds(tmp_path: Path) -> None:
    """The login tool must read credentials out of the vault and drive
    the browser without leaking secret values into observations."""
    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    vault_ref = f"grants/{uuid.uuid4()}"
    vault.put(str(tenant_id), vault_ref, {"username": "alice", "password": "rabbit"})

    sess = FakeBrowserSession(evaluate_responses=discovery_response())
    pool = FakeBrowserPool(next_sessions=[sess])

    # Three steps: login → inspect → done.
    llm = FakeLLMClient()
    llm.tool_steps.extend(
        [
            ToolStep(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="login_with_grant",
                        arguments={
                            "login_url": "https://example.test/login",
                            "account_alias": "primary",
                        },
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
                            "rationale": "logged in via plan-time tool",
                            "dag": _good_dag(),
                        },
                    )
                ],
                final_content=None,
            ),
        ]
    )

    grants = {WEB_LOGIN_REF: {"primary": {"vault_ref": vault_ref, "input_defaults": {}}}}
    service = _service(llm=llm, pool=pool, vault=vault)
    resp = await service.plan(
        user_message="log in to example.test",
        tenant_id=tenant_id,
        granted_capabilities=set(),
        granted_capability_grants=grants,
    )
    assert isinstance(resp, DagResponse)

    # The vault values reached the browser via fill().
    fill_pairs = {(c[1]["selector"], c[1]["value"]) for c in sess.calls if c[0] == "fill"}
    assert ("input[name='username']", "alice") in fill_pairs
    assert ("input[name='password']", "rabbit") in fill_pairs


@pytest.mark.asyncio
async def test_login_refuses_when_grant_missing(tmp_path: Path) -> None:
    """If the LLM tries login_with_grant against an alias that doesn't
    exist, the tool returns an error result; the loop continues and we
    eventually see clarify."""
    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    sess = FakeBrowserSession()
    pool = FakeBrowserPool(next_sessions=[sess])

    llm = FakeLLMClient()
    llm.tool_steps.extend(
        [
            ToolStep(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="login_with_grant",
                        arguments={
                            "login_url": "https://example.test/login",
                            "account_alias": "nonexistent",
                        },
                    )
                ],
                final_content=None,
            ),
            # After getting an error, the LLM gives up and asks the user.
            ToolStep(
                tool_calls=[
                    ToolCall(
                        id="c2",
                        name="done",
                        arguments={
                            "kind": "missing",
                            "rationale": "no grant",
                            "needed": ["cap.web_login"],
                            "explanation": "Set up a cap.web_login grant first.",
                        },
                    )
                ],
                final_content=None,
            ),
        ]
    )

    service = _service(llm=llm, pool=pool, vault=vault)
    resp = await service.plan(
        user_message="log in",
        tenant_id=tenant_id,
        granted_capabilities=set(),
        granted_capability_grants={},  # no grants
    )
    assert isinstance(resp, MissingResponse)
    # No fill should have happened — login bailed out.
    assert all(c[0] != "fill" for c in sess.calls)


@pytest.mark.asyncio
async def test_login_aborts_on_captcha(tmp_path: Path) -> None:
    """Plan-time login must NOT try to solve captchas — the tool returns
    an error and the LLM is expected to compose a runtime DAG instead."""
    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    vault_ref = f"grants/{uuid.uuid4()}"
    vault.put(str(tenant_id), vault_ref, {"username": "u", "password": "p"})

    sess = FakeBrowserSession(
        evaluate_responses=discovery_response(
            captcha_image="img.captcha",
            captcha_input="input[name='captcha']",
            captcha_kind="image",
        )
    )
    pool = FakeBrowserPool(next_sessions=[sess])

    llm = FakeLLMClient()
    llm.tool_steps.extend(
        [
            ToolStep(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="login_with_grant",
                        arguments={
                            "login_url": "https://example.test/login",
                            "account_alias": "primary",
                        },
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
                            "rationale": "captcha → defer to runtime cap.web_login",
                            "dag": _good_dag(),
                        },
                    )
                ],
                final_content=None,
            ),
        ]
    )

    grants = {WEB_LOGIN_REF: {"primary": {"vault_ref": vault_ref, "input_defaults": {}}}}
    service = _service(llm=llm, pool=pool, vault=vault)
    resp = await service.plan(
        user_message="log in",
        tenant_id=tenant_id,
        granted_capabilities=set(),
        granted_capability_grants=grants,
    )
    assert isinstance(resp, DagResponse)
    # No actual fill happened — captcha aborted plan-time login.
    assert all(c[0] != "fill" for c in sess.calls)
