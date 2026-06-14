"""Recording event validation (privacy contract) + draft-DAG compilation."""

from __future__ import annotations

import pytest

from aakaar.capabilities import load_into
from aakaar.interpreter.activities.registry import ActivityRegistry
from aakaar.services.recordings.compiler import (
    MAX_COMPILED_NODES,
    EmptyRecording,
    EventContractViolation,
    compile_recording,
    parse_events,
)
from aakaar.shared.dag.types import NodeKind
from aakaar.shared.dag.validator import validate_dag
from aakaar.shared.registry import build_default_registry

ALIAS = "lab-1"


def _ev(kind: str, data: dict, t: int = 0) -> dict:
    return {"t": t, "kind": kind, "data": data}


FIXTURE = [
    _ev("window", {"title": "HDFC NetBanking - Chromium", "app": "chromium"}),
    _ev("click", {"x": 120, "y": 240, "button": "left"}, t=500),
    _ev("text", {"count": 12}, t=900),
    _ev("key", {"combo": "tab"}, t=1200),
    _ev("text", {"count": 8}, t=1500),
    _ev("key", {"combo": "enter"}, t=1800),
    _ev("scroll", {"dx": 0, "dy": -240}, t=2500),
]


# ---------- parse_events: contract enforcement -------------------------------


def test_parse_events_accepts_contract_stream() -> None:
    events = parse_events(FIXTURE)
    assert [e.kind for e in events] == [
        "window", "click", "text", "key", "text", "key", "scroll",
    ]


def test_parse_rejects_text_event_with_raw_characters() -> None:
    bad = [_ev("text", {"count": 7, "chars": "hunter2"})]
    with pytest.raises(EventContractViolation) as exc:
        parse_events(bad)
    # The secret must never be echoed back through the error.
    assert "hunter2" not in str(exc.value)


def test_parse_rejects_string_count() -> None:
    with pytest.raises(EventContractViolation):
        parse_events([_ev("text", {"count": "secret"})])


def test_parse_rejects_non_allowlisted_key_combo() -> None:
    with pytest.raises(EventContractViolation, match="allowlist"):
        parse_events([_ev("key", {"combo": "a"})])
    with pytest.raises(EventContractViolation) as exc:
        parse_events([_ev("key", {"combo": "ctrl+shift+p"})])
    assert "ctrl+shift+p" not in str(exc.value)  # message never echoes the combo


def test_parse_normalizes_allowlisted_combo_case() -> None:
    events = parse_events([_ev("key", {"combo": " Ctrl+C "})])
    assert events[0].data.combo == "ctrl+c"  # type: ignore[union-attr]


def test_parse_rejects_unknown_kind_and_extra_fields() -> None:
    with pytest.raises(EventContractViolation):
        parse_events([_ev("keystroke", {"combo": "enter"})])
    with pytest.raises(EventContractViolation):
        parse_events([{"t": 0, "kind": "click", "data": {"x": 1, "y": 2}, "raw": "leak"}])
    with pytest.raises(EventContractViolation):
        parse_events([_ev("click", {"x": 1, "y": 2, "button": "left", "char": "x"})])
    with pytest.raises(EventContractViolation):
        parse_events("not-a-list")
    with pytest.raises(EventContractViolation):
        parse_events([_ev("click", "not-a-dict")])


def test_parse_truncates_window_strings() -> None:
    events = parse_events([_ev("window", {"title": "T" * 999, "app": "A" * 999})])
    data = events[0].data
    assert len(data.title) == 300  # type: ignore[union-attr]
    assert len(data.app) == 120  # type: ignore[union-attr]


# ---------- compile_recording -------------------------------------------------


def test_compile_maps_events_to_capability_nodes() -> None:
    compiled = compile_recording(parse_events(FIXTURE), agent_alias=ALIAS)
    dag = compiled.dag
    assert [n.ref for n in dag.nodes] == [
        "cap.window_manage",
        "cap.desktop_click",
        "cap.desktop_type",
        "cap.key_send",
        "cap.desktop_type",
        "cap.key_send",
        "cap.desktop_scroll",
    ]
    assert all(n.kind is NodeKind.CAPABILITY for n in dag.nodes)
    assert all(n.target == ALIAS for n in dag.nodes)
    # Linear chain.
    assert len(dag.edges) == len(dag.nodes) - 1
    for edge, (src, dst) in zip(
        dag.edges, zip(dag.nodes, dag.nodes[1:], strict=False), strict=True
    ):
        assert edge.source == src.id and edge.target == dst.id
    assert dag.nodes[0].inputs == {
        "action": "activate", "title": "HDFC NetBanking - Chromium",
    }
    assert dag.nodes[1].inputs == {"x": 120, "y": 240, "button": "left"}
    assert dag.nodes[6].inputs == {"dx": 0, "dy": -240}


def test_compile_redacts_text_with_numbered_placeholders() -> None:
    compiled = compile_recording(parse_events(FIXTURE), agent_alias=ALIAS)
    typed = [n for n in compiled.dag.nodes if n.ref == "cap.desktop_type"]
    assert [n.inputs["text"] for n in typed] == [
        "<REPLACE_REDACTED_TEXT_1>",
        "<REPLACE_REDACTED_TEXT_2>",
    ]
    assert any("redacted" in w for w in compiled.warnings)
    # No keystroke counts or raw text appear anywhere in the DAG inputs.
    assert all("count" not in n.inputs for n in compiled.dag.nodes)


def test_compile_skips_repeated_and_empty_window_titles() -> None:
    events = parse_events(
        [
            _ev("window", {"title": "App", "app": "app"}),
            _ev("window", {"title": "App", "app": "app"}),
            _ev("window", {"title": "", "app": "app"}),
            _ev("window", {"title": "Other", "app": "app"}),
        ]
    )
    compiled = compile_recording(events, agent_alias=ALIAS)
    assert [n.inputs["title"] for n in compiled.dag.nodes] == ["App", "Other"]


def test_compile_truncates_at_node_cap_with_warning() -> None:
    events = parse_events([_ev("click", {"x": i, "y": i}, t=i) for i in range(400)])
    compiled = compile_recording(events, agent_alias=ALIAS)
    assert len(compiled.dag.nodes) == MAX_COMPILED_NODES
    assert any(str(MAX_COMPILED_NODES) in w for w in compiled.warnings)


def test_compile_cap_does_not_skip_a_placeholder_number() -> None:
    # A text event that lands exactly on the node cap is dropped, not half-
    # counted: the kept placeholders must stay contiguous (no gap in numbering).
    raw = [_ev("text", {"count": 1}, t=i) for i in range(MAX_COMPILED_NODES + 5)]
    compiled = compile_recording(parse_events(raw), agent_alias=ALIAS)
    placeholders = [n.inputs["text"] for n in compiled.dag.nodes]
    assert placeholders == [
        f"<REPLACE_REDACTED_TEXT_{i}>" for i in range(1, MAX_COMPILED_NODES + 1)
    ]
    # The redaction warning counts exactly the kept steps, not the dropped one.
    assert any(f"{MAX_COMPILED_NODES} typed-text" in w for w in compiled.warnings)


def test_compile_warns_about_coordinate_clicks() -> None:
    compiled = compile_recording(parse_events(FIXTURE), agent_alias=ALIAS)
    assert any("coordinates" in w for w in compiled.warnings)


def test_compile_empty_stream_raises() -> None:
    with pytest.raises(EmptyRecording):
        compile_recording([], agent_alias=ALIAS)
    # Only no-op window events: nothing compilable either.
    with pytest.raises(EmptyRecording):
        compile_recording(
            parse_events([_ev("window", {"title": "", "app": "x"})]), agent_alias=ALIAS
        )


def test_compiled_draft_passes_registry_validation() -> None:
    """A recorded draft (incl. scroll + hotkey steps) must round-trip through
    the registry-validating workflows path. Regression for compiled drafts
    emitting cap.desktop_scroll / cap.key_send refs the server didn't register,
    which made record → edit → PUT /workflows fail with a 422."""
    registry = build_default_registry()
    load_into(registry, ActivityRegistry())
    compiled = compile_recording(parse_events(FIXTURE), agent_alias=ALIAS)

    refs = {n.ref for n in compiled.dag.nodes}
    assert {"cap.desktop_scroll", "cap.key_send"} <= refs
    # Every recorded ref the compiler can emit must resolve in the registry, or
    # the create/update workflow router rejects the draft. This is the layer
    # the recordings stop flow skips; it is what re-saving the draft runs.
    validate_dag(compiled.dag, registry=registry)


def test_recorded_capability_refs_are_registered() -> None:
    """Defense in depth: the exact ref strings the compiler hard-codes must all
    exist in the loaded registry as remote capabilities, so adding a new event
    kind without its server-side contract stub fails loudly here."""
    registry = build_default_registry()
    load_into(registry, ActivityRegistry())
    for ref in (
        "cap.window_manage",
        "cap.desktop_click",
        "cap.desktop_type",
        "cap.desktop_scroll",
        "cap.key_send",
    ):
        defn = registry.get(ref)
        assert defn is not None, f"compiler emits {ref!r} but registry lacks it"
        assert defn.kind is NodeKind.CAPABILITY
