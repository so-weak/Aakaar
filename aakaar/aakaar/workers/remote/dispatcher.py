"""RemoteDispatcher — run one capability node on a remote agent.

Called by the executor when a node's ``target`` selects an agent. It resolves a
suitable online agent (placement), builds the task (resolved inputs + a
just-in-time credential envelope fetched from the vault), dispatches under a
deadline, audits which agent ran it, and maps the result back into the node's
outputs (or raises so the executor's normal failure/retry path handles it).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aakaar.interpreter.activities.types import ActivityContext
from aakaar.interpreter.credentials import fetch_credentials
from aakaar.shared.dag.types import Node
from aakaar.workers.remote.protocol import RemoteTask, new_task_id
from aakaar.workers.remote.registry import AgentRegistry, NoAgentAvailable

logger = logging.getLogger(__name__)


class RemoteExecError(RuntimeError):
    """A remote node could not be placed, timed out, or failed on the agent."""


class RemoteDispatcher:
    def __init__(
        self,
        *,
        agents: AgentRegistry,
        registry: Any = None,
        audit: Any = None,
        recorder: Any = None,
        default_timeout_s: float = 300.0,
    ) -> None:
        self._agents = agents
        self._defs = registry  # capability registry (for tags/secrets), optional
        self._audit = audit
        self._recorder = recorder  # event recorder, for run-timeline provenance
        self._timeout = default_timeout_s

    async def run(
        self,
        node: Node,
        inputs: dict[str, Any],
        ctx: ActivityContext,
        target: str | None = None,
    ) -> dict[str, Any]:
        defn = self._defs.get(node.ref) if self._defs is not None else None
        require_gui = "gui" in tuple(getattr(defn, "tags", ()) or ())
        # Explicit `target` (the run-level/effective placement) wins; fall back
        # to the node's own target.
        target = target or node.target or "server"
        try:
            conn = self._agents.resolve(
                ctx.tenant_id, target, ref=node.ref, require_gui=require_gui
            )
        except NoAgentAvailable as e:
            raise RemoteExecError(
                f"node {node.id!r} ({node.ref}) cannot be placed: {e}"
            ) from e

        secrets = self._collect_secrets(node, inputs, ctx, defn)
        task = RemoteTask(
            task_id=new_task_id(),
            run_id=str(ctx.run_id),
            node_id=node.id,
            ref=node.ref,
            inputs=inputs,
            secrets=secrets,
            timeout_s=self._timeout,
        )
        agent_alias = conn.info.alias
        logger.info(
            "remote dispatch run=%s node=%s ref=%s -> agent=%s",
            ctx.run_id,
            node.id,
            node.ref,
            agent_alias,
        )
        # Run-timeline provenance: visible to anyone who can view the run (the
        # audit log is admin-only). The frontend reads `agent` for the badge.
        if self._recorder is not None:
            try:
                self._recorder.record(
                    run_id=ctx.run_id,
                    tenant_id=ctx.tenant_id,
                    node_id=node.id,
                    kind="log",
                    payload={"message": f"running on agent {agent_alias}", "agent": agent_alias},
                )
            except Exception:  # pragma: no cover - provenance must not break a run
                logger.debug("remote provenance event failed", exc_info=True)
        try:
            result = await asyncio.wait_for(
                conn.dispatch(task), timeout=self._timeout + 5.0
            )
        except TimeoutError as e:
            self._audit_dispatch(ctx, node, agent_alias, ok=False, note="timeout")
            raise RemoteExecError(
                f"node {node.id!r} timed out on agent {agent_alias!r}"
            ) from e
        except ConnectionError as e:
            self._audit_dispatch(ctx, node, agent_alias, ok=False, note="disconnected")
            raise RemoteExecError(
                f"node {node.id!r} lost its agent {agent_alias!r}: {e}"
            ) from e

        self._audit_dispatch(ctx, node, agent_alias, ok=result.ok)
        if not result.ok:
            err = result.error or {}
            raise RemoteExecError(
                f"node {node.id!r} failed on agent {agent_alias!r}: "
                f"{err.get('message', 'remote error')}"
            )
        return result.outputs

    def _collect_secrets(
        self, node: Node, inputs: dict[str, Any], ctx: ActivityContext, defn: Any
    ) -> dict[str, str]:
        """Fetch only the secrets this node needs, just-in-time, to send in the
        task envelope. Nothing is fetched unless the capability declares secrets
        and the node supplies an account_alias."""
        if defn is None or not getattr(defn, "secrets", ()):
            return {}
        alias = inputs.get("account_alias")
        if not isinstance(alias, str) or not alias:
            return {}
        try:
            return dict(fetch_credentials(ctx, capability_ref=node.ref, account_alias=alias))
        except PermissionError as e:
            raise RemoteExecError(f"node {node.id!r}: {e}") from e

    def _audit_dispatch(
        self,
        ctx: ActivityContext,
        node: Node,
        agent_alias: str,
        *,
        ok: bool,
        note: str | None = None,
    ) -> None:
        if self._audit is None:
            return
        try:
            self._audit.record(
                action="remote.dispatch",
                tenant_id=ctx.tenant_id,
                target_kind="run",
                target_id=str(ctx.run_id),
                payload={
                    "node": node.id,
                    "ref": node.ref,
                    "agent": agent_alias,
                    "ok": ok,
                    "note": note,
                },
            )
        except Exception:  # pragma: no cover - audit must never break a run
            logger.debug("remote dispatch audit failed", exc_info=True)


__all__ = ["RemoteDispatcher", "RemoteExecError"]
