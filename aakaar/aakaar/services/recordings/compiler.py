"""Recorded-event validation + draft-DAG compilation.

The agent's `cap.activity_recording` capability returns a privacy-reduced
event stream on stop: clicks/scrolls/window switches verbatim, keyboard input
collapsed to an allowlisted set of navigation hotkeys plus redacted-character
counts. This module is the server-side enforcement point for that contract
(`parse_events`) and the compiler that turns a validated stream into a linear
draft workflow DAG (`compile_recording`).

Privacy stance: raw keystrokes never reach the server. Any `text` event that
carries more than a count, or any `key` event whose combo is not allowlisted,
fails validation and the whole stop is rejected — we never compile, persist,
or echo suspect payloads.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator

from aakaar.shared.dag.types import Dag, Edge, Node, NodeKind
from aakaar.shared.dag.validator import validate_dag

# Navigation/hotkey combos the agent may report verbatim. Anything else is
# keyboard *content* and must arrive aggregated into `text` counts.
ALLOWED_KEY_COMBOS = frozenset(
    {
        "enter",
        "tab",
        "esc",
        "ctrl+a",
        "ctrl+c",
        "ctrl+v",
        "ctrl+s",
        "ctrl+tab",
        "alt+tab",
        "shift+tab",
    }
)

MAX_COMPILED_NODES = 300
WINDOW_TITLE_MAX = 300
WINDOW_APP_MAX = 120


class EventContractViolation(ValueError):
    """The agent sent events outside the privacy contract. Messages are built
    without echoing payload values so suspect content can't leak via errors."""


class EmptyRecording(ValueError):
    """The recording produced no compilable steps."""


# ---------- event schemas (extra='forbid' everywhere: unexpected fields are
# ---------- treated as potential raw-input leakage, not tolerated) ----------


class ClickData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: int
    y: int
    button: Literal["left", "right", "middle"] = "left"


class ScrollData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dx: int
    dy: int


class KeyData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    combo: str

    @field_validator("combo")
    @classmethod
    def _allowlisted(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in ALLOWED_KEY_COMBOS:
            # Deliberately does not echo the combo — it may be raw input.
            raise ValueError("key combo is not in the navigation allowlist")
        return normalized


class TextData(BaseModel):
    """Redacted keystrokes: a count only. StrictInt so a string can never
    masquerade as a count."""

    model_config = ConfigDict(extra="forbid")

    count: StrictInt = Field(ge=1)


class WindowData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = ""
    app: str = ""

    @field_validator("title")
    @classmethod
    def _trim_title(cls, v: str) -> str:
        return v.strip()[:WINDOW_TITLE_MAX]

    @field_validator("app")
    @classmethod
    def _trim_app(cls, v: str) -> str:
        return v.strip()[:WINDOW_APP_MAX]


_DATA_MODELS: dict[str, type[BaseModel]] = {
    "click": ClickData,
    "scroll": ScrollData,
    "key": KeyData,
    "text": TextData,
    "window": WindowData,
}

EventKind = Literal["click", "scroll", "key", "text", "window"]


class RecordedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    t: int = Field(ge=0)
    """Milliseconds since recording start."""
    kind: EventKind
    data: ClickData | ScrollData | KeyData | TextData | WindowData


def _sanitized_error(e: ValidationError) -> str:
    """Render a ValidationError without input values (which may be raw text)."""
    parts = []
    for err in e.errors(include_url=False, include_context=False, include_input=False):
        loc = ".".join(str(seg) for seg in err.get("loc", ()))
        parts.append(f"{loc or '<root>'}: {err.get('msg', 'invalid')}")
    return "; ".join(parts)


def parse_events(raw: object) -> list[RecordedEvent]:
    """Validate the agent's event stream against the privacy contract.

    Raises EventContractViolation on the first bad event. Validation is
    all-or-nothing: a stream containing any out-of-contract event is rejected
    wholesale rather than partially compiled.
    """
    if not isinstance(raw, list):
        raise EventContractViolation("agent returned a non-list events payload")
    events: list[RecordedEvent] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise EventContractViolation(f"event #{i} is not an object")
        if extra := set(item) - {"t", "kind", "data"}:
            raise EventContractViolation(
                f"event #{i} carries unexpected fields ({len(extra)})"
            )
        kind = item.get("kind")
        model = _DATA_MODELS.get(kind) if isinstance(kind, str) else None
        if model is None:
            raise EventContractViolation(f"event #{i} has unknown kind")
        data = item.get("data")
        if not isinstance(data, dict):
            raise EventContractViolation(f"event #{i} ({kind}) has no data object")
        try:
            parsed_data = model.model_validate(data)
            events.append(
                RecordedEvent(t=item.get("t", 0), kind=kind, data=parsed_data)
            )
        except ValidationError as e:
            raise EventContractViolation(
                f"event #{i} ({kind}) violates the recording contract: {_sanitized_error(e)}"
            ) from e
    return events


# ---------- compilation ------------------------------------------------------
#
# This compiler is an original implementation in this repo's idioms. A diverged
# sibling fork (Aakaar-Ravi) also compiles desktop recordings into DAGs; this
# code was written independently against the agent's privacy-reduced event
# contract and does not reuse its node-emission code. Where behaviour overlaps
# (linear chaining, coordinate/redaction warnings) it follows from the shared
# capability surface, not from copied source.

REDACTED_TEXT_PLACEHOLDER = "<REPLACE_REDACTED_TEXT_{n}>"


@dataclass(frozen=True, slots=True)
class CompiledRecording:
    dag: Dag
    warnings: list[str]
    rationale: str


@dataclass
class _CompileState:
    """Mutable bookkeeping threaded through the event handlers."""

    last_window: str = ""
    redacted: int = 0
    clicks: int = 0


# Each handler turns one validated event into a (ref, inputs) capability node,
# or returns None to skip it (e.g. a redundant window switch). The dispatch
# table keeps the loop below flat and makes the event->capability mapping the
# obvious thing to read and extend.
_Emit = tuple[str, dict[str, Any]] | None


def _on_window(data: WindowData, st: _CompileState) -> _Emit:
    if not data.title or data.title == st.last_window:
        return None
    st.last_window = data.title
    return "cap.window_manage", {"action": "activate", "title": data.title}


def _on_click(data: ClickData, st: _CompileState) -> _Emit:
    st.clicks += 1
    return "cap.desktop_click", {"x": data.x, "y": data.y, "button": data.button}


def _on_scroll(data: ScrollData, st: _CompileState) -> _Emit:
    return "cap.desktop_scroll", {"dx": data.dx, "dy": data.dy}


def _on_key(data: KeyData, st: _CompileState) -> _Emit:
    return "cap.key_send", {"combo": data.combo}


def _on_text(data: TextData, st: _CompileState) -> _Emit:
    st.redacted += 1
    return "cap.desktop_type", {
        "text": REDACTED_TEXT_PLACEHOLDER.format(n=st.redacted),
    }


_HANDLERS: dict[type, Callable[[Any, _CompileState], _Emit]] = {
    WindowData: _on_window,
    ClickData: _on_click,
    ScrollData: _on_scroll,
    KeyData: _on_key,
    TextData: _on_text,
}


def compile_recording(
    events: list[RecordedEvent], *, agent_alias: str
) -> CompiledRecording:
    """Compile a validated event stream into a linear draft DAG.

    Every node targets the agent the recording came from. Typed text becomes a
    cap.desktop_type node with a <REPLACE_REDACTED_TEXT_n> placeholder the user
    must fill in. Raises EmptyRecording when nothing compilable was captured.
    """
    state = _CompileState()
    pending: list[tuple[str, dict[str, Any]]] = []
    capped = False

    for event in events:
        handler = _HANDLERS.get(type(event.data))
        if handler is None:  # pragma: no cover - parse_events restricts the union
            continue
        emit = handler(event.data, state)
        if emit is None:
            continue
        # The cap counts compiled nodes, not raw events: a long run of repeated
        # window switches that all collapse should not eat the budget. A text
        # event that is dropped here must not advance the redaction counter, so
        # roll it back rather than letting the placeholder index skip a number.
        if len(pending) >= MAX_COMPILED_NODES:
            capped = True
            if isinstance(event.data, TextData):
                state.redacted -= 1
            break
        pending.append(emit)

    if not pending:
        raise EmptyRecording("the recording contained no compilable desktop actions")

    nodes = [
        Node(
            id=f"rec_{i:03d}",
            kind=NodeKind.CAPABILITY,
            ref=ref,
            inputs=inputs,
            target=agent_alias,
        )
        for i, (ref, inputs) in enumerate(pending, start=1)
    ]
    edges = [
        Edge(source=src.id, target=dst.id)
        for src, dst in zip(nodes, nodes[1:], strict=False)
    ]

    warnings = _build_warnings(state, capped=capped)

    dag = Dag(nodes=nodes, edges=edges)
    validate_dag(dag)  # structural layer; refs are agent-side capabilities

    rationale = (
        f"Draft compiled from {len(events)} desktop events recorded on agent "
        f"{agent_alias!r}. Window switches, clicks, scrolls and allowlisted "
        "navigation hotkeys map one-to-one onto desktop capabilities; typed text "
        "was redacted on the agent and appears only as placeholders. Review and "
        "edit every step before running."
    )
    return CompiledRecording(dag=dag, warnings=warnings, rationale=rationale)


def _build_warnings(state: _CompileState, *, capped: bool) -> list[str]:
    warnings: list[str] = []
    if capped:
        warnings.append(
            f"The recording compiled to more than {MAX_COMPILED_NODES} steps; "
            f"only the first {MAX_COMPILED_NODES} were kept and the rest dropped."
        )
    if state.clicks:
        warnings.append(
            "Clicks were captured as raw screen coordinates; verify them against "
            "the target machine's layout before running."
        )
    if state.redacted:
        warnings.append(
            f"{state.redacted} typed-text step(s) were redacted at capture. Replace "
            "each <REPLACE_REDACTED_TEXT_n> placeholder with the intended text "
            "before running."
        )
    return warnings


__all__ = [
    "ALLOWED_KEY_COMBOS",
    "MAX_COMPILED_NODES",
    "CompiledRecording",
    "EmptyRecording",
    "EventContractViolation",
    "RecordedEvent",
    "compile_recording",
    "parse_events",
]
