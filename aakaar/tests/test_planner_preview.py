"""Tests for the deterministic plan preview + risk tiers (planner/preview.py).

The preview is DETERMINISTIC (no LLM): topologically-ordered steps, a
conservative per-step risk tier, a `requires_human` flag for HITL steps, and a
risk rollup that drives `needs_confirmation`.
"""

from __future__ import annotations

from pydantic import BaseModel

from aakaar.planner.preview import PlanPreview, RiskTier, summarize_dag
from aakaar.shared.dag.types import Dag, Edge, Node, NodeKind
from aakaar.shared.registry import CapabilityDefinition, Registry, build_default_registry


class _In(BaseModel):
    pass


class _Out(BaseModel):
    pass


def _edge(a: str, b: str) -> Edge:
    return Edge.model_validate({"from": a, "to": b})


def _registry() -> Registry:
    reg = build_default_registry()
    reg.add(
        CapabilityDefinition(
            ref="cap.web_login",
            description="Log into a portal using stored credentials.",
            input_schema=_In,
            output_schema=_Out,
            side_effecting=False,
            tags=("auth",),
        )
    )
    reg.add(
        CapabilityDefinition(
            ref="cap.payment_transfer",
            description="Transfer money to a beneficiary.",
            input_schema=_In,
            output_schema=_Out,
            side_effecting=True,
            tags=("money",),
        )
    )
    reg.add(
        CapabilityDefinition(
            ref="cap.file_upload",
            description="Upload a file to a portal form.",
            input_schema=_In,
            output_schema=_Out,
            side_effecting=True,
        )
    )
    return reg


# ---------- ordering -------------------------------------------------------


def test_steps_are_topologically_ordered() -> None:
    reg = _registry()
    # Declared out of order; edges impose open -> go -> close.
    dag = Dag(
        nodes=[
            Node(id="close", kind=NodeKind.ACTION, ref="browser.close_session", inputs={"session": "s"}),
            Node(id="go", kind=NodeKind.ACTION, ref="browser.navigate", inputs={"session": "s", "url": "https://x"}),
            Node(id="open", kind=NodeKind.ACTION, ref="browser.open_session"),
        ],
        edges=[_edge("open", "go"), _edge("go", "close")],
    )
    preview = summarize_dag(dag, reg)
    assert [s.node_id for s in preview.steps] == ["open", "go", "close"]
    assert [s.order for s in preview.steps] == [1, 2, 3]


def test_preview_never_raises_on_cyclic_dag() -> None:
    # The validator rejects cycles, but preview must stay total — it falls back
    # to declared order for leftover nodes.
    reg = _registry()
    dag = Dag(
        nodes=[
            Node(id="a", kind=NodeKind.ACTION, ref="browser.open_session"),
            Node(id="b", kind=NodeKind.ACTION, ref="browser.navigate", inputs={"session": "s", "url": "https://x"}),
        ],
        edges=[_edge("a", "b"), _edge("b", "a")],
    )
    preview = summarize_dag(dag, reg)
    assert {s.node_id for s in preview.steps} == {"a", "b"}


# ---------- risk tiers -----------------------------------------------------


def test_read_only_workflow_needs_no_confirmation() -> None:
    reg = _registry()
    dag = Dag(
        nodes=[
            Node(id="open", kind=NodeKind.ACTION, ref="browser.open_session"),
            Node(id="go", kind=NodeKind.ACTION, ref="browser.navigate", inputs={"session": "s", "url": "https://x"}),
            Node(id="ext", kind=NodeKind.ACTION, ref="browser.extract", inputs={"session": "s", "selector": "#x"}),
        ],
        edges=[_edge("open", "go"), _edge("go", "ext")],
    )
    preview = summarize_dag(dag, reg)
    assert preview.highest_risk == RiskTier.READ.value
    assert all(s.risk == RiskTier.READ.value for s in preview.steps)
    assert preview.needs_confirmation is False
    assert preview.requires_human is False


def test_write_step_promotes_confirmation() -> None:
    reg = _registry()
    dag = Dag(
        nodes=[
            Node(id="open", kind=NodeKind.ACTION, ref="browser.open_session"),
            Node(id="set_amt", kind=NodeKind.ACTION, ref="browser.set_field", inputs={"session": "s", "label": "Amount", "value": "10"}),
        ],
        edges=[_edge("open", "set_amt")],
    )
    preview = summarize_dag(dag, reg)
    risks = {s.node_id: s.risk for s in preview.steps}
    assert risks["open"] == RiskTier.READ.value
    assert risks["set_amt"] == RiskTier.WRITE.value
    assert preview.highest_risk == RiskTier.WRITE.value
    assert preview.needs_confirmation is True


def test_destructive_capability_is_high_risk() -> None:
    reg = _registry()
    dag = Dag(nodes=[Node(id="pay", kind=NodeKind.CAPABILITY, ref="cap.payment_transfer")])
    preview = summarize_dag(dag, reg)
    assert preview.steps[0].risk == RiskTier.HIGH_RISK.value
    assert preview.highest_risk == RiskTier.HIGH_RISK.value
    assert preview.needs_confirmation is True


def test_side_effecting_capability_without_destructive_keyword_is_write() -> None:
    reg = _registry()
    dag = Dag(nodes=[Node(id="upload", kind=NodeKind.CAPABILITY, ref="cap.file_upload")])
    preview = summarize_dag(dag, reg)
    assert preview.steps[0].risk == RiskTier.WRITE.value
    assert preview.highest_risk == RiskTier.WRITE.value


def test_read_only_capability_is_read() -> None:
    reg = _registry()
    dag = Dag(nodes=[Node(id="login", kind=NodeKind.CAPABILITY, ref="cap.web_login")])
    preview = summarize_dag(dag, reg)
    assert preview.steps[0].risk == RiskTier.READ.value


# ---------- human-in-the-loop ----------------------------------------------


def test_human_prompt_requires_human_and_confirmation() -> None:
    reg = _registry()
    dag = Dag(
        nodes=[
            Node(id="open", kind=NodeKind.ACTION, ref="browser.open_session"),
            Node(id="confirm", kind=NodeKind.CONTROL, ref="human.prompt", inputs={"message": "OK?", "expects": "confirm"}),
        ],
        edges=[_edge("open", "confirm")],
    )
    preview = summarize_dag(dag, reg)
    human_step = next(s for s in preview.steps if s.node_id == "confirm")
    assert human_step.requires_human is True
    assert preview.requires_human is True
    # A human gate needs confirmation even if every step is otherwise READ.
    assert preview.needs_confirmation is True


# ---------- rollup ---------------------------------------------------------


def test_risk_counts_and_rollup() -> None:
    reg = _registry()
    dag = Dag(
        nodes=[
            Node(id="open", kind=NodeKind.ACTION, ref="browser.open_session"),  # read
            Node(id="set_amt", kind=NodeKind.ACTION, ref="browser.set_field", inputs={"session": "s", "label": "A", "value": "1"}),  # write
            Node(id="pay", kind=NodeKind.CAPABILITY, ref="cap.payment_transfer"),  # high_risk
        ],
        edges=[_edge("open", "set_amt"), _edge("set_amt", "pay")],
    )
    preview = summarize_dag(dag, reg)
    assert isinstance(preview, PlanPreview)
    assert preview.risk_counts == {"read": 1, "write": 1, "high_risk": 1}
    assert preview.highest_risk == RiskTier.HIGH_RISK.value


def test_summary_echoes_notable_input_but_never_a_bare_ref() -> None:
    reg = _registry()
    dag = Dag(
        nodes=[
            Node(id="open", kind=NodeKind.ACTION, ref="browser.open_session"),
            Node(
                id="go",
                kind=NodeKind.ACTION,
                ref="browser.navigate",
                inputs={"session": "${open.session}", "url": "https://bank.example.com"},
            ),
        ],
        edges=[_edge("open", "go")],
    )
    preview = summarize_dag(dag, reg)
    go = next(s for s in preview.steps if s.node_id == "go")
    # The literal URL surfaces; the ${open.session} plumbing does not.
    assert "https://bank.example.com" in go.summary
    assert "${" not in go.summary


def test_deterministic_repeated_calls_match() -> None:
    reg = _registry()
    dag = Dag(
        nodes=[
            Node(id="open", kind=NodeKind.ACTION, ref="browser.open_session"),
            Node(id="go", kind=NodeKind.ACTION, ref="browser.navigate", inputs={"session": "s", "url": "https://x"}),
        ],
        edges=[_edge("open", "go")],
    )
    first = summarize_dag(dag, reg)
    second = summarize_dag(dag, reg)
    assert [(s.node_id, s.risk, s.order) for s in first.steps] == [
        (s.node_id, s.risk, s.order) for s in second.steps
    ]
