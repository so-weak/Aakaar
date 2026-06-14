"""Tests for the DAG validator."""

from __future__ import annotations

import pytest

from aakaar.shared.dag import Dag, Edge, Node, NodeKind, ValidationError, validate_dag
from aakaar.shared.registry import build_default_registry

# ---------- helpers --------------------------------------------------------


def _node(nid: str, ref: str = "browser.navigate", **kwargs: object) -> Node:
    return Node(id=nid, kind=NodeKind.ACTION, ref=ref, **kwargs)  # type: ignore[arg-type]


def _edge(a: str, b: str) -> Edge:
    return Edge.model_validate({"from": a, "to": b})


# ---------- structural -----------------------------------------------------


def test_empty_dag_rejected() -> None:
    with pytest.raises(ValidationError, match="at least one node"):
        validate_dag(Dag(nodes=[]))


def test_duplicate_node_id() -> None:
    with pytest.raises(ValidationError, match="duplicate node id"):
        validate_dag(Dag(nodes=[_node("a"), _node("a")]))


def test_edge_endpoints_must_exist() -> None:
    with pytest.raises(ValidationError, match="edge source"):
        validate_dag(Dag(nodes=[_node("a")], edges=[_edge("missing", "a")]))
    with pytest.raises(ValidationError, match="edge target"):
        validate_dag(Dag(nodes=[_node("a")], edges=[_edge("a", "missing")]))


def test_cycle_detected() -> None:
    dag = Dag(
        nodes=[_node("a"), _node("b")],
        edges=[_edge("a", "b"), _edge("b", "a")],
    )
    with pytest.raises(ValidationError, match="cycle"):
        validate_dag(dag)


def test_alias_collision() -> None:
    dag = Dag(
        nodes=[
            _node("a", outputs_as="shared"),
            _node("b", outputs_as="shared"),
        ]
    )
    with pytest.raises(ValidationError, match="collides"):
        validate_dag(dag)


# ---------- ref reachability ----------------------------------------------


def test_ref_must_point_upstream() -> None:
    # b references a, but no edge from a to b — b's rank is not after a.
    dag = Dag(
        nodes=[
            _node("a", outputs_as="src"),
            _node("b", inputs={"url": "${src}"}),
        ]
    )
    with pytest.raises(ValidationError, match="not upstream"):
        validate_dag(dag)


def test_ref_with_edge_succeeds() -> None:
    dag = Dag(
        nodes=[
            _node("a"),
            _node("b", inputs={"url": "${a}"}),
        ],
        edges=[_edge("a", "b")],
    )
    validate_dag(dag)


def test_ref_to_unknown_alias() -> None:
    dag = Dag(
        nodes=[_node("a", inputs={"url": "${ghost.x}"})],
    )
    with pytest.raises(ValidationError, match="unknown alias"):
        validate_dag(dag)


def test_ref_to_self_rejected() -> None:
    dag = Dag(
        nodes=[_node("a", inputs={"url": "${a.x}"})],
    )
    with pytest.raises(ValidationError, match="references itself"):
        validate_dag(dag)


# ---------- registry-aware checks -----------------------------------------


def test_registry_unknown_ref() -> None:
    reg = build_default_registry()
    dag = Dag(nodes=[_node("a", ref="bogus.thing")])
    with pytest.raises(ValidationError, match="not in the registry"):
        validate_dag(dag, registry=reg)


def test_registry_kind_mismatch() -> None:
    reg = build_default_registry()
    # human.prompt is a control node, not an action.
    bad = Node(id="a", kind=NodeKind.ACTION, ref="human.prompt", inputs={"message": "hi"})
    with pytest.raises(ValidationError, match="registered as"):
        validate_dag(Dag(nodes=[bad]), registry=reg)


def test_registry_unknown_input() -> None:
    reg = build_default_registry()
    dag = Dag(
        nodes=[
            Node(
                id="a",
                kind=NodeKind.ACTION,
                ref="browser.navigate",
                inputs={"session": "s", "url": "x", "extra": "nope"},
            )
        ]
    )
    with pytest.raises(ValidationError, match="unknown input"):
        validate_dag(dag, registry=reg)


def test_registry_missing_required_input() -> None:
    reg = build_default_registry()
    dag = Dag(
        nodes=[
            Node(
                id="a",
                kind=NodeKind.ACTION,
                ref="browser.navigate",
                inputs={"session": "s"},  # missing url
            )
        ]
    )
    with pytest.raises(ValidationError, match="missing required input"):
        validate_dag(dag, registry=reg)


def test_registry_ref_head_validated() -> None:
    reg = build_default_registry()
    # browser.open_session declares output `session`. Asking for `${a.cookies}` is wrong.
    dag = Dag(
        nodes=[
            Node(id="a", kind=NodeKind.ACTION, ref="browser.open_session"),
            Node(
                id="b",
                kind=NodeKind.ACTION,
                ref="browser.navigate",
                inputs={"session": "${a.cookies}", "url": "https://x"},
            ),
        ],
        edges=[_edge("a", "b")],
    )
    with pytest.raises(ValidationError, match="only declares outputs"):
        validate_dag(dag, registry=reg)


def test_full_valid_pipeline() -> None:
    reg = build_default_registry()
    dag = Dag(
        nodes=[
            Node(id="open", kind=NodeKind.ACTION, ref="browser.open_session"),
            Node(
                id="go",
                kind=NodeKind.ACTION,
                ref="browser.navigate",
                inputs={"session": "${open.session}", "url": "https://abc.com"},
            ),
            Node(
                id="dl",
                kind=NodeKind.ACTION,
                ref="browser.download",
                inputs={"session": "${open.session}", "trigger_selector": "#pdf"},
            ),
        ],
        edges=[_edge("open", "go"), _edge("go", "dl")],
    )
    validate_dag(dag, registry=reg)


# ---------- tenant grants -------------------------------------------------


def test_capability_grant_required() -> None:
    reg = build_default_registry()
    # We don't have any capabilities in the default registry, so let's add a
    # synthetic one for the test by going through the registry directly.
    from pydantic import BaseModel as _BM

    from aakaar.shared.registry.types import CapabilityDefinition

    class _In(_BM):
        pass

    class _Out(_BM):
        pass

    reg.add(
        CapabilityDefinition(
            ref="cap.test_login",
            description="test",
            input_schema=_In,
            output_schema=_Out,
        )
    )

    dag = Dag(
        nodes=[Node(id="a", kind=NodeKind.CAPABILITY, ref="cap.test_login")]
    )
    with pytest.raises(ValidationError, match="not granted"):
        validate_dag(dag, registry=reg, granted_capabilities=set())

    validate_dag(dag, registry=reg, granted_capabilities={"cap.test_login"})
