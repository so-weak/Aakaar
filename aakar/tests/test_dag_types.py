"""Tests for DAG type validation and serialization."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydValidationError

from aakar.shared.dag import Dag, Edge, Node, NodeKind


def test_node_basic_round_trip() -> None:
    n = Node(id="n1", kind=NodeKind.ACTION, ref="browser.navigate", inputs={"url": "x"})
    assert n.id == "n1"
    assert n.kind is NodeKind.ACTION

    dumped = n.model_dump()
    assert dumped["ref"] == "browser.navigate"
    assert Node.model_validate(dumped) == n


def test_node_id_validation() -> None:
    with pytest.raises(PydValidationError):
        Node(id="", kind=NodeKind.ACTION, ref="browser.navigate")
    with pytest.raises(PydValidationError):
        Node(id="1n", kind=NodeKind.ACTION, ref="browser.navigate")
    with pytest.raises(PydValidationError):
        Node(id="n-1", kind=NodeKind.ACTION, ref="browser.navigate")


def test_node_ref_validation() -> None:
    with pytest.raises(PydValidationError):
        Node(id="n1", kind=NodeKind.ACTION, ref="Browser.Navigate")
    with pytest.raises(PydValidationError):
        Node(id="n1", kind=NodeKind.ACTION, ref="browser")  # needs at least one dot
    Node(id="n1", kind=NodeKind.ACTION, ref="browser.navigate")
    Node(id="n1", kind=NodeKind.CAPABILITY, ref="cap.foo_bar.baz")


def test_node_extra_fields_forbidden() -> None:
    with pytest.raises(PydValidationError):
        Node.model_validate(
            {
                "id": "n1",
                "kind": "action",
                "ref": "browser.navigate",
                "secret": "shhh",
            }
        )


def test_edge_alias_round_trip() -> None:
    e = Edge.model_validate({"from": "a", "to": "b"})
    assert e.source == "a" and e.target == "b"
    assert e.model_dump(by_alias=True) == {"from": "a", "to": "b"}


def test_dag_round_trip() -> None:
    d = Dag(
        nodes=[
            Node(id="a", kind=NodeKind.ACTION, ref="browser.navigate"),
            Node(id="b", kind=NodeKind.ACTION, ref="browser.click"),
        ],
        edges=[Edge.model_validate({"from": "a", "to": "b"})],
    )
    raw = d.model_dump(by_alias=True)
    parsed = Dag.model_validate(raw)
    assert parsed == d
