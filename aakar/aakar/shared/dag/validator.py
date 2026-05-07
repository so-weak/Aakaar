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

from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel

from aakar.shared.dag.refs import Ref, parse_refs
from aakar.shared.dag.types import Dag, Node, NodeKind


class ValidationError(ValueError):
    """A DAG validation failure. The string is intentionally LLM-friendly so it
    can be fed back as repair context."""


# ---------- registry protocol ----------------------------------------------


class DefinitionLike(Protocol):
    """Minimal shape the validator needs from a registry entry."""

    ref: str
    kind: NodeKind
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]


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


def validate_dag(
    dag: Dag,
    *,
    registry: RegistryLike | None = None,
    granted_capabilities: set[str] | None = None,
) -> None:
    """Validate a DAG. Raises ValidationError on the first problem.

    Layer 1 (always): structural integrity — unique ids, valid edges, no cycles,
                      and every ref points to an upstream node.
    Layer 2 (registry): every node ref exists; inputs match schemas; every
                        ${alias.head} resolves to a declared output field.
    Layer 3 (tenant): every capability node has been granted to this tenant.
    """
    if not dag.nodes:
        raise ValidationError("DAG must contain at least one node")

    idx = _build_index(dag)
    _topological_order(idx)  # raises if there's a cycle

    for node in dag.nodes:
        ancestors = _ancestors_of(node.id, idx)
        for ref_path, ref in parse_refs(node.inputs):
            if ref.alias not in idx.alias_to_id:
                raise ValidationError(
                    f"node {node.id!r} input{_path_str(ref_path)} references unknown alias "
                    f"{ref.alias!r}"
                )
            src_id = idx.alias_to_id[ref.alias]
            if src_id == node.id:
                raise ValidationError(
                    f"node {node.id!r} input{_path_str(ref_path)} references itself"
                )
            if src_id not in ancestors:
                raise ValidationError(
                    f"node {node.id!r} input{_path_str(ref_path)} references {ref.alias!r} "
                    f"which is not upstream (no edge path)"
                )

    if registry is not None:
        _validate_registry(dag, idx, registry)

    if granted_capabilities is not None:
        _validate_grants(dag, granted_capabilities)


def _validate_registry(dag: Dag, idx: _DagIndex, registry: RegistryLike) -> None:
    for node in dag.nodes:
        defn = registry.get(node.ref)
        if defn is None:
            raise ValidationError(f"node {node.id!r} ref {node.ref!r} is not in the registry")
        if defn.kind is not node.kind:
            raise ValidationError(
                f"node {node.id!r} declares kind {node.kind.value!r} but ref {node.ref!r} "
                f"is registered as {defn.kind.value!r}"
            )
        _check_inputs_shape(node, defn)
        _check_ref_heads(node, idx, registry)


def _check_inputs_shape(node: Node, defn: DefinitionLike) -> None:
    """Field-name-level shape check. Skips type validation of values bound to
    refs, since their concrete type is only known at runtime."""
    schema = defn.input_schema
    fields = schema.model_fields
    for key in node.inputs:
        if key not in fields:
            raise ValidationError(
                f"node {node.id!r} ({node.ref!r}) has unknown input {key!r}; "
                f"valid inputs: {sorted(fields)}"
            )
    for fname, finfo in fields.items():
        if finfo.is_required() and fname not in node.inputs:
            raise ValidationError(
                f"node {node.id!r} ({node.ref!r}) is missing required input {fname!r}"
            )


def _check_ref_heads(node: Node, idx: _DagIndex, registry: RegistryLike) -> None:
    """Validate that every ${alias.head} refers to a real output field on the
    source node's ref. Deeper paths are runtime-resolved."""
    for ref_path, ref in parse_refs(node.inputs):
        if not ref.path:
            continue
        src_id = idx.alias_to_id[ref.alias]
        src_node = idx.by_id[src_id]
        src_defn = registry.get(src_node.ref)
        if src_defn is None:
            continue  # already reported by ref-existence check
        out_fields = src_defn.output_schema.model_fields
        if ref.head not in out_fields:
            raise ValidationError(
                f"node {node.id!r} input{_path_str(ref_path)} references "
                f"{ref.alias}.{ref.head!r} but {src_node.ref!r} only declares outputs "
                f"{sorted(out_fields)}"
            )


def _validate_grants(dag: Dag, granted: set[str]) -> None:
    for node in dag.nodes:
        if node.kind is NodeKind.CAPABILITY and node.ref not in granted:
            raise ValidationError(
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


__all__ = ["DefinitionLike", "RegistryLike", "ValidationError", "validate_dag"]


# Re-imported for forward-ref typing
_ = Ref
