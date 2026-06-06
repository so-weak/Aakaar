"""Topological layering for parallel execution.

A DAG validator has already proved acyclicity; here we just bucket nodes
into "layers" — each layer's nodes have no remaining dependencies on
later layers, so they can run concurrently.

This is a straight Kahn's algorithm with the indegree refresh batched
per layer instead of per node, which is what gives us the layered output.
"""

from __future__ import annotations

from aakaar.shared.dag.types import Dag, Node


def topological_layers(dag: Dag) -> list[list[Node]]:
    """Return nodes grouped into parallel layers in execution order.

    Each layer is a list of nodes that depend only on earlier layers.
    """
    indeg: dict[str, int] = {n.id: 0 for n in dag.nodes}
    successors: dict[str, list[str]] = {n.id: [] for n in dag.nodes}
    by_id: dict[str, Node] = {n.id: n for n in dag.nodes}
    for e in dag.edges:
        successors[e.source].append(e.target)
        indeg[e.target] += 1

    layers: list[list[Node]] = []
    ready = [nid for nid, d in indeg.items() if d == 0]
    placed = 0
    while ready:
        layer = sorted(ready)
        layers.append([by_id[nid] for nid in layer])
        placed += len(layer)
        next_ready: list[str] = []
        for nid in layer:
            for s in successors[nid]:
                indeg[s] -= 1
                if indeg[s] == 0:
                    next_ready.append(s)
        ready = next_ready

    if placed != len(dag.nodes):
        # Should never trigger — validator ran first. Defensive.
        raise ValueError("topological layering failed; DAG has a cycle")
    return layers
