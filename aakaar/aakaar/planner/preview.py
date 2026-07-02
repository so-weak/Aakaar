"""Plain-English plan preview + risk flags for a DAG (P0 — banking safety).

Turns a validated (or draft) `Dag` into an ordered, human-readable list of
steps an operator can confirm *before* a run starts — plus a risk rollup so
side-effecting or human-in-the-loop steps are visible up front.

This is deterministic (no LLM): it reads the registry for each node's
description and derives a conservative risk tier, orders nodes topologically,
and classifies each step. The result is safe to compute on every draft render
and to surface in a run-confirmation dialog.

Risk heuristic (conservative — a false WRITE is annoying, a false READ is
dangerous):
  - HIGH_RISK — a side-effecting entry whose ref/tags mention a destructive,
                irreversible, or money-moving action (delete/kill/pay/transfer/
                reject/…). Blast-radius keywords in the ref alone also count.
  - WRITE     — anything else that mutates the target: side_effecting True, an
                UNDECLARED side_effecting (None → treat as write, matching the
                dry-run safe-default), or a ref/tags with a mutation keyword
                (upload/send/submit/fill/click/set_field/write/…).
  - READ      — declared read-only AND no mutation keyword.

The current registry's ``CapabilityDefinition`` exposes ``side_effecting`` and
``tags`` (there is no explicit ``risk_tier`` field), so those two plus the ref
keywords drive the tier. ``human.*`` refs and a ``hitl`` tag mark a step as
requiring a human.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from aakaar.shared.dag.types import Dag, Edge, Node
from aakaar.shared.registry import Registry


class RiskTier(StrEnum):
    """Conservative risk classification for a single plan step.

    Ordered READ < WRITE < HIGH_RISK for "highest risk in this plan" rollups.
    Local to the preview module — the registry does not carry an explicit
    ``risk_tier``; we derive it here from ``side_effecting`` + ``tags`` + ref
    keywords.
    """

    READ = "read"
    WRITE = "write"
    HIGH_RISK = "high_risk"


# Risk ordering for "highest risk in this plan" rollups.
_RISK_ORDER = {RiskTier.READ: 0, RiskTier.WRITE: 1, RiskTier.HIGH_RISK: 2}

# Destructive / irreversible / money-moving keywords. A side-effecting entry
# (or a ref) mentioning any of these is HIGH_RISK, not merely WRITE.
_HIGH_RISK_HINTS = (
    "delete",
    "remove",
    "drop",
    "kill",
    "terminate",
    "pay",
    "payment",
    "transfer",
    "remit",
    "reject",
    "approve",
    "wipe",
    "purge",
    "shell_exec",
    "script_run",
    "power_manage",
    "service_manage",
    "process_manage",
    "release_lot",
    "release",
)

# Mutation keywords: anything that plausibly changes the target's state.
_WRITE_HINTS = (
    "upload",
    "send",
    "write",
    "submit",
    "form_fill",
    "form_autofill",
    "fill_form",
    "set_field",
    "set_",
    "fill",
    "type",
    "click",
    "drag",
    "key_send",
    "notify",
    "webhook",
    "create",
    "update",
    "post",
    "put",
    "patch",
)

# Inputs worth echoing in a step summary (in priority order). These are the
# plain-English "what" of a step; never secrets (the planner never puts
# secrets in inputs).
_NOTABLE_INPUTS = (
    "url",
    "target_hint",
    "menu_path",
    "text",
    "label",
    "value",
    "fields",
    "path",
    "file_uri",
    "to",
    "subject",
    "submit_label",
    "message",
    "expects",
    "query",
    "selector",
)

_REF_RE = re.compile(r"\$\{[^}]+\}")


@dataclass(frozen=True, slots=True)
class PlanStep:
    order: int
    node_id: str
    ref: str
    kind: str
    summary: str
    risk: str  # RiskTier value ("read" | "write" | "high_risk")
    requires_human: bool
    in_cleanup: bool


@dataclass(frozen=True, slots=True)
class PlanPreview:
    steps: list[PlanStep] = field(default_factory=list)
    risk_counts: dict[str, int] = field(default_factory=dict)
    highest_risk: str = RiskTier.READ.value
    requires_human: bool = False
    needs_confirmation: bool = False
    """True when the plan contains any write / high-risk / human-in-the-loop
    step — the UI should ask for an explicit confirm before running."""


def summarize_dag(dag: Dag, registry: Registry) -> PlanPreview:
    """Build a `PlanPreview` for `dag` using the registry for descriptions
    and derived risk tiers. Nodes are topologically ordered.

    Deterministic and total: never raises, even on a malformed/cyclic DAG (the
    topo sort falls back to declared order for any leftover nodes). This lets
    the UI preview a draft the validator would reject."""
    ordered = _topo_order(dag.nodes, dag.edges)
    steps: list[PlanStep] = []
    for n, node in enumerate(ordered, start=1):
        steps.append(_step_for(node, registry, order=n, in_cleanup=False))

    risk_counts: dict[str, int] = {t.value: 0 for t in RiskTier}
    for s in steps:
        risk_counts[s.risk] = risk_counts.get(s.risk, 0) + 1
    highest = _highest_risk(steps)
    requires_human = any(s.requires_human for s in steps)
    needs_confirmation = requires_human or highest in (
        RiskTier.WRITE.value,
        RiskTier.HIGH_RISK.value,
    )
    return PlanPreview(
        steps=steps,
        risk_counts=risk_counts,
        highest_risk=highest,
        requires_human=requires_human,
        needs_confirmation=needs_confirmation,
    )


# ---------- internals ------------------------------------------------------


def _step_for(node: Node, registry: Registry, *, order: int, in_cleanup: bool) -> PlanStep:
    defn = registry.get(node.ref)
    summary = _summarize_node(node, defn)
    risk = _risk_for(node, defn)
    return PlanStep(
        order=order,
        node_id=node.id,
        ref=node.ref,
        kind=node.kind.value,
        summary=summary,
        risk=risk.value,
        requires_human=_is_human_step(node, defn),
        in_cleanup=in_cleanup,
    )


def _summarize_node(node: Node, defn: Any | None) -> str:
    base = _first_sentence(getattr(defn, "description", "")) if defn is not None else node.ref
    if not base:
        base = node.ref
    detail = _notable_input(node.inputs)
    if detail:
        return f"{base} ({detail})"
    return base


def _notable_input(inputs: dict[str, Any]) -> str:
    """Pick one human-meaningful, non-reference input to show as context."""
    for key in _NOTABLE_INPUTS:
        if key not in inputs:
            continue
        val = inputs[key]
        rendered = _render_value(val)
        if rendered:
            return f"{key}: {rendered}"
    return ""


def _render_value(val: Any) -> str:
    if isinstance(val, bool):
        return str(val)
    if isinstance(val, str):
        # Skip pure reference values like "${login.session}" — they're
        # plumbing, not operator-meaningful context.
        if _REF_RE.fullmatch(val.strip()):
            return ""
        s = val.strip()
        return _truncate(s)
    if isinstance(val, list):
        parts = [str(v) for v in val if isinstance(v, (str, int, float))]
        return _truncate(" -> ".join(parts)) if parts else ""
    if isinstance(val, dict):
        # e.g. web_form_fill fields {label: value} — show the labels.
        keys = [str(k) for k in val]
        return _truncate(", ".join(keys)) if keys else ""
    if isinstance(val, (int, float)):
        return str(val)
    return ""


def _truncate(s: str, limit: int = 70) -> str:
    s = s.replace("\n", " ").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _first_sentence(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    # Split on the first sentence-ending period followed by a space, but keep
    # abbreviations like "e.g." intact by requiring the period to end a word of
    # length > 1.
    m = re.search(r"(?<=[a-z0-9)\]])\.\s", text)
    first = text[: m.start() + 1] if m else text
    return _truncate(first, limit=160)


def _risk_for(node: Node, defn: Any | None) -> RiskTier:
    """Conservative risk tier from ``side_effecting`` + ``tags`` + ref keywords.

    The registry has no explicit ``risk_tier``; if a future definition grows
    one, honour it first. Otherwise:

      - a destructive/money-moving keyword in the ref or tags → HIGH_RISK
        (unless the entry is explicitly declared read-only, in which case the
        keyword still earns a WRITE — worth confirming);
      - ``side_effecting is True`` OR a mutation keyword → WRITE;
      - everything else → READ.

    We deliberately do NOT auto-promote ``side_effecting=None`` (undeclared) to
    WRITE: nearly every built-in action leaves it unset, and doing so would
    flag benign reads (navigate, screenshot, extract) as writes. The keyword
    signal is the discriminator; ``side_effecting=True`` is a hard WRITE floor.
    """
    explicit = getattr(defn, "risk_tier", None)
    if isinstance(explicit, RiskTier):
        return explicit

    ref = node.ref.lower()
    tags = tuple(str(t).lower() for t in (getattr(defn, "tags", ()) or ()))
    haystack = (ref, *tags)

    def _mentions(hints: tuple[str, ...]) -> bool:
        return any(h in item for h in hints for item in haystack)

    side_effecting = getattr(defn, "side_effecting", None)

    # Destructive / irreversible / money-moving → HIGH_RISK, unless the entry is
    # explicitly declared read-only (then keep it at WRITE — still confirm-worthy).
    if _mentions(_HIGH_RISK_HINTS):
        return RiskTier.WRITE if side_effecting is False else RiskTier.HIGH_RISK

    # Declared side-effecting, or a mutation keyword in the ref/tags.
    if side_effecting is True or _mentions(_WRITE_HINTS):
        return RiskTier.WRITE

    return RiskTier.READ


def _is_human_step(node: Node, defn: Any | None) -> bool:
    if node.ref.startswith("human."):
        return True
    tags = getattr(defn, "tags", ()) or ()
    return "hitl" in tuple(str(t).lower() for t in tags)


def _highest_risk(steps: list[PlanStep]) -> str:
    if not steps:
        return RiskTier.READ.value
    best = max(steps, key=lambda s: _RISK_ORDER.get(RiskTier(s.risk), 0))
    return best.risk


def _topo_order(nodes: list[Node], edges: list[Edge]) -> list[Node]:
    """Kahn topological sort, preserving declared order among ready nodes.

    Falls back to declared order for any nodes left over (e.g. if a cycle
    slipped through — the validator should have rejected it, but preview must
    never raise)."""
    by_id = {n.id: n for n in nodes}
    indeg: dict[str, int] = {n.id: 0 for n in nodes}
    adj: dict[str, list[str]] = {n.id: [] for n in nodes}
    for e in edges:
        src = getattr(e, "source", None)
        tgt = getattr(e, "target", None)
        if src in by_id and tgt in by_id:
            adj[src].append(tgt)
            indeg[tgt] += 1

    order_index = {n.id: i for i, n in enumerate(nodes)}
    ready = sorted((nid for nid, d in indeg.items() if d == 0), key=lambda x: order_index[x])
    out: list[Node] = []
    seen: set[str] = set()
    while ready:
        nid = ready.pop(0)
        if nid in seen:
            continue
        seen.add(nid)
        out.append(by_id[nid])
        newly: list[str] = []
        for m in adj[nid]:
            indeg[m] -= 1
            if indeg[m] == 0:
                newly.append(m)
        for m in sorted(newly, key=lambda x: order_index[x]):
            ready.append(m)
    # Append any leftovers (cycle guard) in declared order.
    for n in nodes:
        if n.id not in seen:
            out.append(n)
    return out


__all__ = ["PlanPreview", "PlanStep", "RiskTier", "summarize_dag"]
