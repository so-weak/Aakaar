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
from aakaar.shared.dag import ValidationError
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
    """A DAG whose ONLY problem is an ungranted capability short-circuits to a
    `missing` result (no repair looping) naming the needed cap — turning what
    used to be a dead-end 502 into an actionable "grant this" message."""
    reg = _registry_with("cap.requires_grant")
    dag = Dag(
        nodes=[
            Node(id="login", kind=NodeKind.CAPABILITY, ref="cap.requires_grant"),
        ]
    )
    llm = FakeLLMClient(replies=[PlannerCompletion(kind="dag", dag=dag)] * 3)
    service = PlannerService(registry=reg, llm=llm, max_repair_attempts=2)
    resp = service.plan(user_message="x", granted_capabilities=set())
    assert isinstance(resp, MissingResponse)
    assert resp.needed == ["cap.requires_grant"]
    assert "cap.requires_grant" in resp.explanation
    # Short-circuited on the first attempt — no repair rounds burned.
    assert len(llm.calls) == 1

    # When granted, the same DAG validates.
    llm2 = FakeLLMClient(replies=[PlannerCompletion(kind="dag", dag=dag)])
    service2 = PlannerService(registry=reg, llm=llm2)
    resp2 = service2.plan(
        user_message="x", granted_capabilities={"cap.requires_grant"}
    )
    assert isinstance(resp2, DagResponse)


def test_multiple_ungranted_caps_collapse_to_one_missing() -> None:
    """Several ungranted caps in one DAG surface together in a single `missing`
    result — no per-cap repair rounds."""
    reg = build_default_registry()
    for ref in ("cap.a_login", "cap.b_login"):
        reg.add(
            CapabilityDefinition(
                ref=ref, description="x", input_schema=_In, output_schema=_Out
            )
        )
    dag = Dag(
        nodes=[
            Node(id="a", kind=NodeKind.CAPABILITY, ref="cap.a_login"),
            Node(id="b", kind=NodeKind.CAPABILITY, ref="cap.b_login"),
        ]
    )
    llm = FakeLLMClient(replies=[PlannerCompletion(kind="dag", dag=dag)])
    service = PlannerService(registry=reg, llm=llm, max_repair_attempts=2)
    resp = service.plan(user_message="x", granted_capabilities=set())
    assert isinstance(resp, MissingResponse)
    assert resp.needed == ["cap.a_login", "cap.b_login"]
    assert len(llm.calls) == 1


def test_all_errors_fed_back_in_one_repair_round() -> None:
    """A DAG with several independent validation problems has ALL of them fed
    back in the SAME repair message (collect-all), so the model can fix every
    issue in one round instead of one-per-round whack-a-mole."""
    reg = _registry_with()
    bad = Dag(
        nodes=[
            # missing required `url`
            Node(id="go", kind=NodeKind.ACTION, ref="browser.navigate", inputs={"session": "s"}),
            # unknown input `bogus`  AND missing required `url`+`session`? navigate
            # requires session+url; here we give an unknown input to a second node.
            Node(
                id="dl",
                kind=NodeKind.ACTION,
                ref="browser.download",
                inputs={"session": "s", "nonsense": "x"},
            ),
        ]
    )
    llm = FakeLLMClient(
        replies=[
            PlannerCompletion(kind="dag", dag=bad),
            PlannerCompletion(kind="dag", dag=_valid_dag()),
        ]
    )
    service = PlannerService(registry=reg, llm=llm, max_repair_attempts=2)
    resp = service.plan(user_message="x", granted_capabilities=set())
    assert isinstance(resp, DagResponse)
    # Exactly one repair round was needed.
    assert len(llm.calls) == 2
    repair_msg = "\n".join(m.content for m in llm.calls[1])
    # Both problems appear in the single repair message.
    assert "missing required input 'url'" in repair_msg
    assert "unknown input 'nonsense'" in repair_msg
    # And the hints carry an actionable FIX clause.
    assert "FIX:" in repair_msg


def test_malformed_completion_is_repairable_not_fatal() -> None:
    """A malformed LLM completion (surfaced as a ValidationError from the
    client) is fed back to the model rather than 502-ing immediately."""

    from aakaar.planner.llm import PlannerCompletion as _PC

    class _FlakyClient:
        def __init__(self) -> None:
            self.calls: list[object] = []
            self._n = 0

        def complete_planner(self, messages: list[object]) -> _PC:
            self.calls.append(messages)
            self._n += 1
            if self._n == 1:
                raise ValidationError(
                    "Your previous reply was not a valid PlannerCompletion."
                )
            return _PC(kind="dag", dag=_valid_dag(), rationale="ok")

        def complete_with_tools(self, messages, tools):  # pragma: no cover
            raise NotImplementedError

        def complete_text(self, system, user):  # pragma: no cover
            return ""

    reg = _registry_with()
    client = _FlakyClient()
    service = PlannerService(registry=reg, llm=client, max_repair_attempts=2)
    resp = service.plan(user_message="x", granted_capabilities=set())
    assert isinstance(resp, DagResponse)
    assert len(client.calls) == 2
