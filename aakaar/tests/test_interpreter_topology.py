"""Topological-layering tests."""

from __future__ import annotations

from aakaar.interpreter.topology import topological_layers
from aakaar.shared.dag.types import Dag, Edge, Node, NodeKind


def _node(nid: str) -> Node:
    return Node(id=nid, kind=NodeKind.ACTION, ref="browser.navigate")


def _edge(a: str, b: str) -> Edge:
    return Edge.model_validate({"from": a, "to": b})


def test_linear_dag() -> None:
    dag = Dag(
        nodes=[_node("a"), _node("b"), _node("c")],
        edges=[_edge("a", "b"), _edge("b", "c")],
    )
    layers = topological_layers(dag)
    assert [[n.id for n in layer] for layer in layers] == [["a"], ["b"], ["c"]]


def test_parallel_layer() -> None:
    dag = Dag(
        nodes=[_node("a"), _node("b"), _node("c"), _node("d")],
        edges=[_edge("a", "c"), _edge("a", "d"), _edge("b", "c"), _edge("b", "d")],
    )
    layers = topological_layers(dag)
    assert [[n.id for n in layer] for layer in layers] == [["a", "b"], ["c", "d"]]


def test_independent_nodes_collapse_into_one_layer() -> None:
    dag = Dag(nodes=[_node("a"), _node("b"), _node("c")])
    layers = topological_layers(dag)
    assert len(layers) == 1
    assert sorted(n.id for n in layers[0]) == ["a", "b", "c"]
