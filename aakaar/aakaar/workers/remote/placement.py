"""Pre-flight placement check.

Walks a DAG and reports any remote-targeted node that no currently-online agent
can run (wrong tenant/pool/alias, missing capability, or needs a GUI session the
agents don't have). Powers the editor's inline warnings and the launch-time
availability gate.
"""

from __future__ import annotations

from typing import Any

from aakaar.shared.dag.types import Dag, NodeKind
from aakaar.workers.remote.registry import AgentRegistry, NoAgentAvailable


def check_placement(
    dag: Dag,
    tenant_id: Any,
    *,
    agents: AgentRegistry,
    registry: Any = None,
) -> list[dict[str, str]]:
    """Return a list of unsatisfiable remote nodes: {node_id, ref, target, reason}.
    An empty list means every remote node can currently be placed."""
    issues: list[dict[str, str]] = []
    for node in dag.nodes:
        target = node.target
        if target is None or target == "server":
            continue
        if node.kind is NodeKind.CONTROL:
            issues.append(
                {
                    "node_id": node.id,
                    "ref": node.ref,
                    "target": target,
                    "reason": "control nodes must run on the server",
                }
            )
            continue
        defn = registry.get(node.ref) if registry is not None else None
        require_gui = "gui" in tuple(getattr(defn, "tags", ()) or ())
        try:
            agents.resolve(tenant_id, target, ref=node.ref, require_gui=require_gui)
        except NoAgentAvailable as e:
            issues.append(
                {
                    "node_id": node.id,
                    "ref": node.ref,
                    "target": target,
                    "reason": str(e),
                }
            )
    return issues


__all__ = ["check_placement"]
