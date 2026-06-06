"""Tests for the planner orchestrator."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from aakaar.planner import (
    FakeLLMClient,
    PlannerCompletion,
    PlannerError,
    PlannerService,
)
from aakaar.shared.dag.types import Dag, Edge, Node, NodeKind
from aakaar.shared.planner.responses import (
    ClarifyResponse,
    DagResponse,
    MissingResponse,
)
from aakaar.shared.registry import (
    CapabilityDefinition,
    Registry,
    SecretSpec,
    build_default_registry,
)


class _In(BaseModel):
    pass


class _Out(BaseModel):
    pass


def _registry_with(ref: str = "cap.test_login") -> Registry:
    reg = build_default_registry()
    reg.add(
        CapabilityDefinition(
            ref=ref,
            description="test capability",
            input_schema=_In,
            output_schema=_Out,
            secrets=(SecretSpec(name="username"),),
        )
    )
    return reg


def _valid_dag() -> Dag:
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


# ---------- happy paths ---------------------------------------------------


def test_dag_response_round_trip() -> None:
    reg = _registry_with()
    llm = FakeLLMClient(replies=[PlannerCompletion(kind="dag", dag=_valid_dag(), rationale="ok")])
    service = PlannerService(registry=reg, llm=llm)

    resp = service.plan(user_message="open and go", granted_capabilities=set())
    assert isinstance(resp, DagResponse)
    assert resp.rationale == "ok"
    assert len(llm.calls) == 1


def test_clarify_response_passthrough() -> None:
    reg = _registry_with()
    llm = FakeLLMClient(
        replies=[PlannerCompletion(kind="clarify", questions=["which account?"])]
    )
    service = PlannerService(registry=reg, llm=llm)
    resp = service.plan(user_message="login", granted_capabilities={"cap.test_login"})
    assert isinstance(resp, ClarifyResponse)
    assert resp.questions == ["which account?"]


def test_missing_response_passthrough() -> None:
    reg = _registry_with()
    llm = FakeLLMClient(
        replies=[
            PlannerCompletion(
                kind="missing",
                needed=["cap.icici_login"],
                explanation="not granted",
            )
        ]
    )
    service = PlannerService(registry=reg, llm=llm)
    resp = service.plan(user_message="login to icici", granted_capabilities=set())
    assert isinstance(resp, MissingResponse)
    assert resp.needed == ["cap.icici_login"]


# ---------- validation + repair -------------------------------------------


def test_invalid_dag_triggers_repair_then_succeeds() -> None:
    reg = _registry_with()
    bad_dag = Dag(
        nodes=[
            Node(
                id="go",
                kind=NodeKind.ACTION,
                ref="browser.navigate",
                inputs={"session": "s"},  # missing required `url`
            )
        ]
    )
    llm = FakeLLMClient(
        replies=[
            PlannerCompletion(kind="dag", dag=bad_dag),
            PlannerCompletion(kind="dag", dag=_valid_dag()),
        ]
    )
    service = PlannerService(registry=reg, llm=llm, max_repair_attempts=2)
    resp = service.plan(user_message="x", granted_capabilities=set())
    assert isinstance(resp, DagResponse)
    assert len(llm.calls) == 2
    # The second call must include the repair feedback message.
    repair_msgs = llm.calls[1]
    assert any("did not validate" in m.content for m in repair_msgs)


def test_exhausted_repair_budget_raises() -> None:
    reg = _registry_with()
    bad = Dag(
        nodes=[
            Node(
                id="go",
                kind=NodeKind.ACTION,
                ref="browser.navigate",
                inputs={"session": "s"},
            )
        ]
    )
    llm = FakeLLMClient(
        replies=[
            PlannerCompletion(kind="dag", dag=bad),
            PlannerCompletion(kind="dag", dag=bad),
            PlannerCompletion(kind="dag", dag=bad),
        ]
    )
    service = PlannerService(registry=reg, llm=llm, max_repair_attempts=2)
    with pytest.raises(PlannerError):
        service.plan(user_message="x", granted_capabilities=set())


def test_capability_grant_enforced_by_validator() -> None:
    """A DAG referencing an ungranted capability must be rejected."""
    reg = _registry_with("cap.requires_grant")
    dag = Dag(
        nodes=[
            Node(id="login", kind=NodeKind.CAPABILITY, ref="cap.requires_grant"),
        ]
    )
    llm = FakeLLMClient(replies=[PlannerCompletion(kind="dag", dag=dag)] * 3)
    service = PlannerService(registry=reg, llm=llm, max_repair_attempts=2)
    with pytest.raises(PlannerError, match="not granted"):
        service.plan(user_message="x", granted_capabilities=set())

    # When granted, the same DAG validates.
    llm2 = FakeLLMClient(replies=[PlannerCompletion(kind="dag", dag=dag)])
    service2 = PlannerService(registry=reg, llm=llm2)
    resp = service2.plan(
        user_message="x", granted_capabilities={"cap.requires_grant"}
    )
    assert isinstance(resp, DagResponse)
