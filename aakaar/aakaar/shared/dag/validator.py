"""DAG validator.

Layered checks:
  1. structural   — id uniqueness, edge endpoints, acyclicity, ref reachability
  2. registry     — every node ref exists; inputs match the declared input schema;
                    every ${alias.head} resolves to a declared output field
  3. tenant       — every capability node has been granted to the tenant

The first layer runs unconditionally. Pass a registry to enable layer 2.
Pass granted capability refs to enable layer 3.

The validator never partially validates: it raises on the first problem and
includes the offending node id / ref in the error so the planner can repair.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel

from aakaar.shared.dag.refs import INPUTS_ALIAS, Ref, parse_refs
from aakaar.shared.dag.types import Dag, Edge, Node, NodeKind


class ValidationError(ValueError):
    """A DAG validation failure. The string is intentionally LLM-friendly so it
    can be fed back as repair context."""


# ---------- registry protocol ----------------------------------------------


class DefinitionLike(Protocol):
    """Minimal shape the validator needs from a registry entry.

    Members are read-only properties (not bare attributes) so frozen
    dataclasses like ``CapabilityDefinition`` structurally satisfy the
    protocol — a mutable protocol attribute would demand a writable field.
    """

    @property
    def ref(self) -> str: ...
    @property
    def kind(self) -> NodeKind: ...
    @property
    def input_schema(self) -> type[BaseModel]: ...
    @property
    def output_schema(self) -> type[BaseModel]: ...


class RegistryLike(Protocol):
    def get(self, ref: str) -> DefinitionLike | None: ...


# ---------- internal index --------------------------------------------------


@dataclass(slots=True)
class _DagIndex:
    by_id: dict[str, Node] = field(default_factory=dict)
    alias_to_id: dict[str, str] = field(default_factory=dict)
    successors: dict[str, list[str]] = field(default_factory=dict)
    predecessors: dict[str, list[str]] = field(default_factory=dict)


def _build_index(dag: Dag) -> _DagIndex:
    idx = _DagIndex()
    for n in dag.nodes:
        if n.id in idx.by_id:
            raise ValidationError(f"duplicate node id {n.id!r}")
        idx.by_id[n.id] = n
        idx.alias_to_id[n.id] = n.id  # node id is always usable as an alias
        if n.outputs_as is not None:
            if n.outputs_as in idx.alias_to_id and idx.alias_to_id[n.outputs_as] != n.id:
                raise ValidationError(
                    f"alias {n.outputs_as!r} on node {n.id!r} collides with another node/alias"
                )
            idx.alias_to_id[n.outputs_as] = n.id
        idx.successors.setdefault(n.id, [])
        idx.predecessors.setdefault(n.id, [])
    for e in dag.edges:
        if e.source not in idx.by_id:
            raise ValidationError(f"edge source {e.source!r} is not a node")
        if e.target not in idx.by_id:
            raise ValidationError(f"edge target {e.target!r} is not a node")
        idx.successors[e.source].append(e.target)
        idx.predecessors[e.target].append(e.source)
    return idx


def _topological_order(idx: _DagIndex) -> list[str]:
    indeg = {nid: len(preds) for nid, preds in idx.predecessors.items()}
    queue = [nid for nid, d in indeg.items() if d == 0]
    order: list[str] = []
    while queue:
        nid = queue.pop(0)
        order.append(nid)
        for s in idx.successors[nid]:
            indeg[s] -= 1
            if indeg[s] == 0:
                queue.append(s)
    if len(order) != len(idx.by_id):
        cyclic = sorted(set(idx.by_id) - set(order))
        raise ValidationError(f"DAG has a cycle involving nodes {cyclic}")
    return order


def _ancestors_of(nid: str, idx: _DagIndex) -> set[str]:
    seen: set[str] = set()
    stack = list(idx.predecessors[nid])
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(idx.predecessors[cur])
    return seen


# ---------- public API -----------------------------------------------------


def auto_complete_edges(dag: Dag) -> Dag:
    """Return a copy of `dag` with implicit data-flow edges materialized.

    Every `${alias.field}` reference in a node's inputs implies a
    "must run after" relationship: the producing node must complete
    before the consuming node starts. Forcing the planner to mirror
    those references in the `edges` list is busywork — the LLM
    consistently forgets, and the validator then rejects what is
    semantically a perfectly fine DAG.

    This pass also strips duplicate `outputs_as` aliases (keeping
    only the first occurrence). The LLM occasionally tags multiple
    nodes with `outputs_as="session"` as if it were a "currently-
    active session" marker; the second tag is invalid (aliases must
    be unique) and the right semantic is to clear it.

    This pass walks every reference; if there is no edge path from
    the producer to the consumer yet, it adds a direct edge. It
    refuses to add an edge that would introduce a cycle (that's a
    real DAG bug the caller should see) — those references fall
    through to `validate_dag` and surface as the original error.
    """
    if not dag.nodes:
        return dag

    # First pass: dedup outputs_as. The first node to claim a name
    # keeps it; later nodes lose theirs.
    seen_aliases: set[str] = set()
    for n in dag.nodes:
        seen_aliases.add(n.id)
    cleaned_nodes: list[Node] = []
    for n in dag.nodes:
        if n.outputs_as is None:
            cleaned_nodes.append(n)
            continue
        # If the alias collides with another node's id, or with an
        # earlier node's outputs_as, strip it.
        if n.outputs_as in seen_aliases:
            cleaned_nodes.append(n.model_copy(update={"outputs_as": None}))
        else:
            seen_aliases.add(n.outputs_as)
            cleaned_nodes.append(n)

    aliases: dict[str, str] = {n.id: n.id for n in cleaned_nodes}
    for n in cleaned_nodes:
        if n.outputs_as is not None and n.outputs_as not in aliases:
            aliases[n.outputs_as] = n.id

    # Mutable adjacency so we can compute reachability after each
    # tentative add without rebuilding the whole index.
    successors: dict[str, set[str]] = {n.id: set() for n in cleaned_nodes}
    predecessors: dict[str, set[str]] = {n.id: set() for n in cleaned_nodes}
    edges: list[Edge] = list(dag.edges)
    for e in edges:
        if e.source in successors and e.target in predecessors:
            successors[e.source].add(e.target)
            predecessors[e.target].add(e.source)

    def _ancestors(nid: str) -> set[str]:
        seen: set[str] = set()
        stack = list(predecessors.get(nid, ()))
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(predecessors.get(cur, ()))
        return seen

    def _descendants(nid: str) -> set[str]:
        seen: set[str] = set()
        stack = list(successors.get(nid, ()))
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(successors.get(cur, ()))
        return seen

    added: list[tuple[str, str]] = []
    for node in cleaned_nodes:
        for _path, ref in parse_refs(node.inputs):
            src_id = aliases.get(ref.alias)
            if src_id is None or src_id == node.id:
                continue
            if src_id in _ancestors(node.id):
                continue
            # Refuse if adding the edge would cycle.
            if src_id in _descendants(node.id) or src_id == node.id:
                continue
            successors[src_id].add(node.id)
            predecessors[node.id].add(src_id)
            added.append((src_id, node.id))

    nodes_changed = any(a is not b for a, b in zip(cleaned_nodes, dag.nodes, strict=True))
    if not added and not nodes_changed:
        return dag
    new_edges = list(dag.edges) + [
        Edge(source=s, target=t) for s, t in added
    ]
    return Dag(
        id=dag.id,
        version=dag.version,
        nodes=cleaned_nodes,
        edges=new_edges,
    )


def validate_dag(
    dag: Dag,
    *,
    registry: RegistryLike | None = None,
    granted_capabilities: set[str] | None = None,
) -> None:
    """Validate a DAG. Raises ValidationError on the first problem.

    Thin wrapper over `validate_dag_collect` that preserves the historical
    raise-on-first-error contract for callers that want a single exception.

    Layer 1 (always): structural integrity — unique ids, valid edges, no cycles,
                      and every ref points to an upstream node.
    Layer 2 (registry): every node ref exists; inputs match schemas; every
                        ${alias.head} resolves to a declared output field.
    Layer 3 (tenant): every capability node has been granted to this tenant.
    """
    errors = validate_dag_collect(
        dag,
        registry=registry,
        granted_capabilities=granted_capabilities,
    )
    if errors:
        raise ValidationError(errors[0])


def validate_dag_collect(
    dag: Dag,
    *,
    registry: RegistryLike | None = None,
    granted_capabilities: set[str] | None = None,
) -> list[str]:
    """Validate a DAG and return EVERY problem found (empty list == valid).

    Same layered checks as `validate_dag`, but instead of raising on the first
    failure it collects all of them so the planner's repair loop can feed the
    whole set back in one round (instead of the old one-error-per-round
    whack-a-mole that exhausted the repair budget).

    Structural blockers (empty DAG, duplicate ids, bad edges, cycles) make
    per-node analysis meaningless, so those still short-circuit: the single
    structural error is returned alone.
    """
    if not dag.nodes:
        return ["DAG must contain at least one node"]

    # Structural layer — one blocker is enough to stop; the rest is noise.
    try:
        idx = _build_index(dag)
        _topological_order(idx)  # raises if there's a cycle
    except ValidationError as e:
        return [str(e)]

    errors: list[str] = []

    # Placement: control nodes (human.prompt, control.wait) coordinate with the
    # server (signals, timers) and must never be shipped to a remote agent.
    for node in dag.nodes:
        if node.kind is NodeKind.CONTROL and node.target not in (None, "server"):
            errors.append(
                f"node {node.id!r} is a control node and cannot run on a remote "
                f"target ({node.target!r}); control flow stays on the server"
            )

    for node in dag.nodes:
        ancestors = _ancestors_of(node.id, idx)
        for ref_path, ref in parse_refs(node.inputs):
            if ref.alias == INPUTS_ALIAS:
                # Run-level inputs namespace: supplied at run start, always
                # available, and not produced by any node — so it needs no
                # upstream edge and no output-field check.
                continue
            if ref.alias not in idx.alias_to_id:
                errors.append(
                    f"node {node.id!r} input{_path_str(ref_path)} references unknown alias "
                    f"{ref.alias!r}"
                )
                continue
            src_id = idx.alias_to_id[ref.alias]
            if src_id == node.id:
                errors.append(
                    f"node {node.id!r} input{_path_str(ref_path)} references itself"
                )
                continue
            if src_id not in ancestors:
                errors.append(
                    f"node {node.id!r} input{_path_str(ref_path)} references {ref.alias!r} "
                    f"which is not upstream (no edge path)"
                )

    if registry is not None:
        _collect_registry_errors(dag, idx, registry, errors)

    if granted_capabilities is not None:
        _collect_grant_errors(dag, granted_capabilities, errors)

    return errors


def _collect_registry_errors(
    dag: Dag, idx: _DagIndex, registry: RegistryLike, errors: list[str]
) -> None:
    for node in dag.nodes:
        defn = registry.get(node.ref)
        if defn is None:
            errors.append(f"node {node.id!r} ref {node.ref!r} is not in the registry")
            continue
        if defn.kind is not node.kind:
            errors.append(
                f"node {node.id!r} declares kind {node.kind.value!r} but ref {node.ref!r} "
                f"is registered as {defn.kind.value!r}"
            )
        _collect_inputs_shape(node, defn, errors)
        _collect_ref_heads(node, idx, registry, errors)


def _collect_inputs_shape(node: Node, defn: DefinitionLike, errors: list[str]) -> None:
    """Field-name-level shape check. Skips type validation of values bound to
    refs, since their concrete type is only known at runtime."""
    schema = defn.input_schema
    fields = schema.model_fields
    for key in node.inputs:
        if key not in fields:
            errors.append(
                f"node {node.id!r} ({node.ref!r}) has unknown input {key!r}; "
                f"valid inputs: {sorted(fields)}"
            )
    for fname, finfo in fields.items():
        if finfo.is_required() and fname not in node.inputs:
            errors.append(
                f"node {node.id!r} ({node.ref!r}) is missing required input {fname!r}"
            )


def _collect_ref_heads(
    node: Node, idx: _DagIndex, registry: RegistryLike, errors: list[str]
) -> None:
    """Validate that every ${alias.head} refers to a real output field on the
    source node's ref. Deeper paths are runtime-resolved."""
    for ref_path, ref in parse_refs(node.inputs):
        if ref.alias == INPUTS_ALIAS:
            continue  # run-inputs fields aren't registry-declared outputs
        if not ref.path:
            continue
        if ref.alias not in idx.alias_to_id:
            continue  # unknown-alias already reported by the wiring pass
        src_id = idx.alias_to_id[ref.alias]
        src_node = idx.by_id[src_id]
        src_defn = registry.get(src_node.ref)
        if src_defn is None:
            continue  # already reported by ref-existence check
        out_fields = src_defn.output_schema.model_fields
        if ref.head not in out_fields:
            errors.append(
                f"node {node.id!r} input{_path_str(ref_path)} references "
                f"{ref.alias}.{ref.head!r} but {src_node.ref!r} only declares outputs "
                f"{sorted(out_fields)}"
            )


def _collect_grant_errors(dag: Dag, granted: set[str], errors: list[str]) -> None:
    for node in dag.nodes:
        if node.kind is NodeKind.CAPABILITY and node.ref not in granted:
            errors.append(
                f"node {node.id!r} uses capability {node.ref!r} which is not granted to this "
                f"tenant"
            )


def _path_str(path: tuple[str | int, ...]) -> str:
    if not path:
        return ""
    parts: list[str] = []
    for seg in path:
        parts.append(f"[{seg}]" if isinstance(seg, int) else f".{seg}")
    return "".join(parts)


# ---------- human/LLM-friendly explanations --------------------------------


# Errors that mean "this capability isn't granted" — the planner special-cases
# them to short-circuit into a `kind="missing"` result instead of burning
# repair attempts on something the LLM can't fix by editing the DAG.
UNGRANTED_MARKER = "is not granted to this tenant"

_UNKNOWN_ALIAS_RE = re.compile(r"references unknown alias '([^']+)'")
_UNKNOWN_REF_RE = re.compile(r"ref '([^']+)' is not in the registry")
_MISSING_INPUT_RE = re.compile(r"is missing required input '([^']+)'")
_UNKNOWN_INPUT_RE = re.compile(r"has unknown input '([^']+)'; valid inputs: (\[[^\]]*\])")
_DANGLING_REF_RE = re.compile(r"references '([^']+)' which is not upstream")


def explain_dag_errors(
    errors: list[str],
    *,
    known_refs: list[str] | None = None,
    known_aliases: list[str] | None = None,
    sample_inputs: dict[str, str] | None = None,
) -> list[str]:
    """Turn raw validation error strings into actionable, LLM-friendly hints.

    Pure function: given the terse error strings from `validate_dag_collect`,
    it appends a concrete "how to fix" clause to each, using optional context:

      - `known_refs`     — every registered ref; an unknown-ref error gets a
                           did-you-mean via difflib.
      - `known_aliases`  — the DAG's node ids / aliases; an unknown-alias or a
                           dangling-`${ref}` error gets a did-you-mean +
                           "add an edge" instruction.
      - `sample_inputs`  — field name → sample value; a missing-required-input
                           error names the field and offers a paste-ready value.

    All context is optional; with none supplied it still emits generic but
    useful guidance and never invents refs/values. Order and count of the
    input errors are preserved (one hint per error).
    """
    refs = known_refs or []
    aliases = known_aliases or []
    samples = sample_inputs or {}
    hints: list[str] = []
    for err in errors:
        hints.append(_explain_one(err, refs, aliases, samples))
    return hints


def _explain_one(
    err: str,
    known_refs: list[str],
    known_aliases: list[str],
    sample_inputs: dict[str, str],
) -> str:
    m = _UNKNOWN_REF_RE.search(err)
    if m:
        bad = m.group(1)
        guess = _closest(bad, known_refs)
        if guess:
            return f"{err} — FIX: there is no ref '{bad}'; did you mean '{guess}'? Use it verbatim."
        return (
            f"{err} — FIX: '{bad}' is not a real ref. Use ONLY refs listed under "
            "'Available capabilities', 'Available action primitives', or "
            "'Available control nodes'. Do not invent refs."
        )

    m = _UNKNOWN_ALIAS_RE.search(err)
    if m:
        bad = m.group(1)
        guess = _closest(bad, known_aliases)
        if guess:
            return (
                f"{err} — FIX: no node has id/alias '{bad}'; did you mean "
                f"'{guess}'? Reference it as ${{{guess}.<field>}}."
            )
        return (
            f"{err} — FIX: '{bad}' is not a node id or an outputs_as alias in this "
            "DAG. Reference only nodes that exist, or add the producing node."
        )

    m = _MISSING_INPUT_RE.search(err)
    if m:
        fname = m.group(1)
        sample = sample_inputs.get(fname)
        if sample is not None:
            return f'{err} — FIX: add input {fname!r}, e.g. "{fname}": {sample}'
        return f"{err} — FIX: add a value for the required input {fname!r} to this node's inputs."

    m = _UNKNOWN_INPUT_RE.search(err)
    if m:
        bad = m.group(1)
        valid = m.group(2)
        return (
            f"{err} — FIX: remove the input {bad!r} — it is not a field on this ref. "
            f"Use only these inputs: {valid}."
        )

    m = _DANGLING_REF_RE.search(err)
    if m:
        producer = m.group(1)
        return (
            f"{err} — FIX: nothing connects '{producer}' to this node, so its output "
            f"may not be ready. Add an edge {{\"from\": \"{producer}\", \"to\": <this node>}} "
            "so it runs first."
        )

    if "references itself" in err:
        return f"{err} — FIX: a node can only read outputs of earlier nodes; point this at a different node."

    if UNGRANTED_MARKER in err:
        return (
            f"{err} — FIX: this capability is not granted to the tenant. Ask an admin to "
            "add the grant, or use a granted capability instead."
        )

    if "is a control node and cannot run on a remote target" in err:
        return f"{err} — FIX: remove the `target` from this control node (control flow stays on the server)."

    # No pattern matched — return the error unchanged so nothing is lost.
    return err


def _closest(word: str, candidates: list[str]) -> str | None:
    import difflib

    matches = difflib.get_close_matches(word, candidates, n=1, cutoff=0.5)
    return matches[0] if matches else None


__all__ = [
    "DefinitionLike",
    "RegistryLike",
    "UNGRANTED_MARKER",
    "ValidationError",
    "explain_dag_errors",
    "validate_dag",
    "validate_dag_collect",
]


# Re-imported for forward-ref typing
_ = Ref
