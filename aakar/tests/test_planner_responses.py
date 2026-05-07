"""Tests for the planner response union."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError as PydValidationError

from aakar.shared.dag import Dag, Node, NodeKind
from aakar.shared.planner import (
    ClarifyResponse,
    DagResponse,
    MissingResponse,
    PlannerResponse,
    PlannerResponseKind,
)


_ADAPTER = TypeAdapter(PlannerResponse)


def test_dag_response_round_trip() -> None:
    dag = Dag(nodes=[Node(id="a", kind=NodeKind.ACTION, ref="browser.open_session")])
    resp = DagResponse(dag=dag, rationale="opens a browser")
    raw = resp.model_dump(mode="json")
    parsed = _ADAPTER.validate_python(raw)
    assert isinstance(parsed, DagResponse)
    assert parsed.dag.nodes[0].id == "a"


def test_clarify_requires_at_least_one_question() -> None:
    with pytest.raises(PydValidationError):
        ClarifyResponse(questions=[])
    ClarifyResponse(questions=["Which account?"])


def test_missing_response() -> None:
    resp = MissingResponse(
        needed=["cap.icici_login"],
        explanation="No granted capability for ICICI yet.",
    )
    assert resp.kind is PlannerResponseKind.MISSING
    raw = resp.model_dump(mode="json")
    parsed = _ADAPTER.validate_python(raw)
    assert isinstance(parsed, MissingResponse)
    assert parsed.needed == ["cap.icici_login"]


def test_discriminator_picks_right_variant() -> None:
    raw_clarify = {"kind": "clarify", "questions": ["What month?"]}
    parsed = _ADAPTER.validate_python(raw_clarify)
    assert isinstance(parsed, ClarifyResponse)

    raw_missing = {"kind": "missing", "needed": ["cap.x"], "explanation": "no"}
    parsed = _ADAPTER.validate_python(raw_missing)
    assert isinstance(parsed, MissingResponse)
