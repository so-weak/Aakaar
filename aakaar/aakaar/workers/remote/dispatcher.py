"""RemoteDispatcher — run one capability node on a remote agent.

Called by the executor when a node's ``target`` selects an agent. It resolves a
suitable online agent (placement), builds the task (resolved inputs + a
just-in-time credential envelope fetched from the vault), dispatches under a
deadline, audits which agent ran it, and maps the result back into the node's
outputs (or raises so the executor's normal failure/retry path handles it).

`invoke` is the run-less variant for one-off control calls (e.g. starting or
stopping an activity recording): same placement + wire path, but no run/node
identity, no credential envelope, and no run-timeline events.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from aakaar.interpreter.activities.types import ActivityContext
from aakaar.interpreter.credentials import fetch_credentials
from aakaar.shared.dag.types import Node
from aakaar.workers.remote.protocol import RemoteTask, new_task_id
from aakaar.workers.remote.registry import AgentRegistry, NoAgentAvailable

logger = logging.getLogger(__name__)


class RemoteExecError(RuntimeError):
    """A remote node could not be placed, timed out, or failed on the agent."""


def is_browser_ref(ref: str) -> bool:
    """Refs that participate in a live browser session — used to pin a run to one
    agent (session affinity) and to gate the remote-browser feature flag."""
    if ref.startswith("browser."):
        return True
    return ref in {"cap.open_url", "cap.web_login", "cap.screenshot", "cap.file_download"} or ref.startswith("cap.web")


class RemoteDispatcher:
    def __init__(
        self,
        *,
        agents: AgentRegistry,
        registry: Any = None,
        audit: Any = None,
        recorder: Any = None,
        default_timeout_s: float = 300.0,
        browser_enabled: bool = False,
        sealer: Any = None,
        live_screenshots: bool = False,
    ) -> None:
        self._agents = agents
        self._defs = registry  # capability registry (for tags/secrets), optional
        self._audit = audit
        self._recorder = recorder  # event recorder, for run-timeline provenance
        self._timeout = default_timeout_s
        # Gate: browser/credential caps run on an agent only when explicitly
        # enabled (independent of remote_exec_enabled).
        self._browser_enabled = browser_enabled
        # Seals the credential envelope to the agent's public key so the broker
        # only relays ciphertext. None disables sealing (cleartext fallback).
        self._sealer = sealer
        self._live_screenshots = live_screenshots

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
        # Browser-family nodes share one live session, so pin the whole run to a
        # single agent (session affinity): the first browser node binds it,
        # later nodes honor the binding.
        sticky = is_browser_ref(node.ref)
        if sticky and not self._browser_enabled:
            raise RemoteExecError(
                f"node {node.id!r} ({node.ref}) targets an agent, but remote browser "
                "execution is disabled — set AAKAAR_REMOTE_BROWSER_ENABLED=1 on the server "
                "to run the browser/credential stack on agents"
            )
        try:
            conn = self._agents.resolve(
                ctx.tenant_id,
                target,
                ref=node.ref,
                require_gui=require_gui,
                run_id=str(ctx.run_id),
                sticky=sticky,
            )
        except NoAgentAvailable as e:
            raise RemoteExecError(
                f"node {node.id!r} ({node.ref}) cannot be placed: {e}"
            ) from e

        secrets = self._collect_secrets(node, inputs, ctx, defn)
        secrets_sealed = None
        if secrets:
            secrets_sealed = self._seal_secrets(secrets, conn)
            if secrets_sealed is not None:
                secrets = {}  # ciphertext only crosses the wire/broker
        task = RemoteTask(
            task_id=new_task_id(),
            run_id=str(ctx.run_id),
            node_id=node.id,
            ref=node.ref,
            inputs=inputs,
            secrets=secrets,
            timeout_s=self._timeout,
            tenant_id=str(ctx.tenant_id),
            secrets_sealed=secrets_sealed,
            live_screen=(self._live_screenshots and sticky),
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

    async def invoke(
        self,
        *,
        tenant_id: uuid.UUID,
        target: str,
        ref: str,
        inputs: dict[str, Any],
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """One-off capability invocation outside any run. Resolves the target
        like `run` and reuses the same task wire format with empty run/node
        identity; the agent treats it as a normal task. No secrets envelope."""
        try:
            conn = self._agents.resolve(tenant_id, target, ref=ref)
        except NoAgentAvailable as e:
            raise RemoteExecError(f"{ref} cannot be placed: {e}") from e
        timeout = self._timeout if timeout_s is None else timeout_s
        task = RemoteTask(
            task_id=new_task_id(),
            run_id="",
            node_id="",
            ref=ref,
            inputs=inputs,
            timeout_s=timeout,
            tenant_id=str(tenant_id),
        )
        agent_alias = conn.info.alias
        logger.info("remote invoke ref=%s -> agent=%s", ref, agent_alias)
        try:
            result = await asyncio.wait_for(conn.dispatch(task), timeout=timeout + 5.0)
        except TimeoutError as e:
            raise RemoteExecError(f"{ref} timed out on agent {agent_alias!r}") from e
        except ConnectionError as e:
            raise RemoteExecError(f"{ref} lost its agent {agent_alias!r}: {e}") from e
        if not result.ok:
            err = result.error or {}
            raise RemoteExecError(
                f"{ref} failed on agent {agent_alias!r}: {err.get('message', 'remote error')}"
            )
        return result.outputs

    def _seal_secrets(self, secrets: dict[str, str], conn: Any) -> dict[str, Any] | None:
        """Seal the credential envelope to the agent's public key. Returns None
        (cleartext fallback) when sealing is unavailable or the agent advertised
        no key — logged loudly because credentials would then cross in cleartext."""
        import json

        pub = getattr(conn.info, "public_key", None)
        if self._sealer is None or not pub:
            logger.warning(
                "remote dispatch: secrets NOT sealed (sealer=%s, agent_key=%s) — "
                "credentials will cross the broker in CLEARTEXT",
                self._sealer is not None,
                bool(pub),
            )
            return None
        return self._sealer.seal(json.dumps(secrets).encode("utf-8"), pub)

    async def end_run(self, tenant_id: uuid.UUID, run_id: str) -> None:
        """On run completion (any terminal status), tell the pinned agent to tear
        down the run's browser session(s) and drop the affinity binding. Best
        effort — a dead socket is fine; the agent reaps on disconnect anyway."""
        alias = self._agents.release_run(tenant_id, run_id)
        if not alias:
            return
        conn = self._agents.get(tenant_id, alias)
        notify = getattr(conn, "notify", None)
        if conn is None or notify is None:
            return
        try:
            await notify("run_end", {"run_id": run_id, "tenant_id": str(tenant_id)})
            logger.info("run_end sent to agent %s for run %s", alias, run_id)
        except Exception:  # pragma: no cover - teardown must never raise
            logger.debug("run_end notify to agent %s failed", alias, exc_info=True)

    def _collect_secrets(
        self, node: Node, inputs: dict[str, Any], ctx: ActivityContext, defn: Any
    ) -> dict[str, str]:
        """Fetch only the secrets this node needs, just-in-time, to send in the
        task envelope. Nothing is fetched unless the capability declares secrets
        and the node supplies an account_alias."""
        # browser.fill_secret is an ACTION (ActionDefinition can't declare
        # secrets), but it resolves a vault secret named in its OWN inputs
        # (capability_ref / account_alias / secret_name). Resolve that grant so
        # the secret travels in the envelope and the shared handler reads it from
        # ctx.secrets — never the vault — when it runs on the agent.
        if node.ref == "browser.fill_secret":
            cap_ref = inputs.get("capability_ref")
            alias = inputs.get("account_alias")
            if not (isinstance(cap_ref, str) and cap_ref and isinstance(alias, str) and alias):
                return {}
            try:
                return dict(fetch_credentials(ctx, capability_ref=cap_ref, account_alias=alias))
            except PermissionError as e:
                raise RemoteExecError(f"node {node.id!r}: {e}") from e

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
