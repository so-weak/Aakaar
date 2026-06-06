"""In-process registry of live agent connections + placement resolution.

Single-node server, so a plain in-memory map is sufficient (no Redis). Keyed by
tenant then alias — agents are tenant-scoped, so a run can only ever reach its
own tenant's agents. Placement resolves a node's ``target`` (an alias or a pool
label) to an online agent that also satisfies the node's requirements
(capability+version, and a GUI session when the capability needs one).
"""

from __future__ import annotations

import logging
import uuid

from aakaar.workers.remote.protocol import AgentConnection, AgentInfo

logger = logging.getLogger(__name__)


class NoAgentAvailable(Exception):
    """No online agent satisfies the placement + requirements."""


class AgentRegistry:
    def __init__(self) -> None:
        self._by_tenant: dict[uuid.UUID, dict[str, AgentConnection]] = {}

    def register(self, conn: AgentConnection) -> None:
        info = conn.info
        self._by_tenant.setdefault(info.tenant_id, {})[info.alias] = conn
        logger.info(
            "agent online tenant=%s alias=%s os=%s gui=%s caps=%d",
            info.tenant_id,
            info.alias,
            info.os,
            info.gui_capable,
            len(info.capabilities),
        )

    def unregister(self, tenant_id: uuid.UUID, alias: str) -> None:
        agents = self._by_tenant.get(tenant_id)
        if agents and alias in agents:
            agents.pop(alias, None)
            logger.info("agent offline tenant=%s alias=%s", tenant_id, alias)
            if not agents:
                self._by_tenant.pop(tenant_id, None)

    def get(self, tenant_id: uuid.UUID, alias: str) -> AgentConnection | None:
        return self._by_tenant.get(tenant_id, {}).get(alias)

    def list_online(self, tenant_id: uuid.UUID) -> list[AgentInfo]:
        return [c.info for c in self._by_tenant.get(tenant_id, {}).values()]

    def _candidates(self, tenant_id: uuid.UUID, target: str) -> list[AgentConnection]:
        agents = self._by_tenant.get(tenant_id, {})
        # Exact alias match wins; otherwise treat target as a pool label.
        if target in agents:
            return [agents[target]]
        return [c for c in agents.values() if c.info.in_pool(target)]

    def resolve(
        self,
        tenant_id: uuid.UUID,
        target: str,
        *,
        ref: str,
        version: str | None = None,
        require_gui: bool = False,
    ) -> AgentConnection:
        """Return an online agent for ``target`` that can run ``ref`` (raises
        NoAgentAvailable with a human-readable reason otherwise)."""
        candidates = self._candidates(tenant_id, target)
        if not candidates:
            raise NoAgentAvailable(
                f"no online agent matches target {target!r} for this tenant"
            )
        viable = [
            c
            for c in candidates
            if c.info.supports(ref, version) and (not require_gui or c.info.gui_capable)
        ]
        if not viable:
            reasons = []
            if not any(c.info.supports(ref, version) for c in candidates):
                reasons.append(f"none support capability {ref!r}")
            if require_gui and not any(c.info.gui_capable for c in candidates):
                reasons.append("none have an interactive GUI session")
            raise NoAgentAvailable(
                f"target {target!r} has {len(candidates)} online agent(s) but "
                + (", ".join(reasons) or "none satisfy the requirements")
            )
        # Deterministic pick (first by alias) — keeps placement stable/testable.
        return sorted(viable, key=lambda c: c.info.alias)[0]


__all__ = ["AgentRegistry", "NoAgentAvailable"]
