"""Tests for the PlannerCompletion envelope's invariants."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydValidationError

from aakaar.planner.llm import PlannerCompletion
from aakaar.shared.dag.types import Dag, Node, NodeKind


def _dag() -> Dag:
    return Dag(nodes=[Node(id="a", kind=NodeKind.ACTION, ref="browser.open_session")])


def test_dag_branch_requires_dag() -> None:
    with pytest.raises(PydValidationError):
        PlannerCompletion(kind="dag")


def test_dag_branch_forbids_clarify_fields() -> None:
    with pytest.raises(PydValidationError):
        PlannerCompletion(kind="dag", dag=_dag(), questions=["wat"])


def test_clarify_branch_requires_questions() -> None:
    with pytest.raises(PydValidationError):
        PlannerCompletion(kind="clarify", questions=[])


def test_clarify_branch_forbids_dag() -> None:
    with pytest.raises(PydValidationError):
        PlannerCompletion(kind="clarify", questions=["?"], dag=_dag())


def test_missing_branch_requires_needed_and_explanation() -> None:
    with pytest.raises(PydValidationError):
        PlannerCompletion(kind="missing", needed=[], explanation="x")
    with pytest.raises(PydValidationError):
        PlannerCompletion(kind="missing", needed=["cap.x"], explanation="")


def test_valid_branches() -> None:
    PlannerCompletion(kind="dag", dag=_dag(), rationale="opens a browser")
    PlannerCompletion(kind="clarify", questions=["Which account?"])
    PlannerCompletion(kind="missing", needed=["cap.x"], explanation="not granted")
