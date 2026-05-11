"""LocalExecutor — async, in-process DAG interpreter.

Walks topological layers, dispatches each node to either:
  - a registered activity handler (most refs)
  - a control-node handler (control.wait, human.prompt) the executor knows
    about directly

Within a layer, nodes run concurrently with `asyncio.gather`. On any node
failure, in-flight peers are allowed to finish (we don't cancel them mid-
flight — they may hold external state like browser sessions that needs
graceful close), but no further layers start.

This is the "Executor" half of the architecture spine. A future
`TemporalExecutor` will satisfy the same Protocol; the rest of the system
(orchestrator, repositories, API) targets the Protocol.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from aakar.db.models import RunEventKind
from aakar.interpreter.activities.registry import ActivityRegistry
from aakar.interpreter.activities.types import ActivityContext
from aakar.interpreter.events import EventRecorder, node_span
from aakar.interpreter.refs import resolve_inputs
from aakar.interpreter.signals import SignalHub
from aakar.interpreter.topology import topological_layers
from aakar.shared.dag.types import Dag, Node, NodeKind


logger = logging.getLogger(__name__)


# ---------- public types --------------------------------------------------


@dataclass
class RunContext:
    run_id: uuid.UUID
    tenant_id: uuid.UUID
    activity_ctx: ActivityContext


@dataclass
class RunOutcome:
    run_id: uuid.UUID
    status: str  # "succeeded" | "failed"
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: dict[str, Any] | None = None


class Executor(Protocol):
    """The execution Protocol. Production swaps `LocalExecutor` for a
    Temporal-backed implementation without touching callers."""

    async def execute(self, dag: Dag, ctx: RunContext) -> RunOutcome: ...


# ---------- LocalExecutor -------------------------------------------------


@dataclass
class LocalExecutor:
    activities: ActivityRegistry
    recorder: EventRecorder
    signals: SignalHub
    llm: Any = None
    """Optional LLM client passed through to capability handlers via
    `ActivityContext.llm`. Capabilities use it for narrow read-only DOM
    introspection (e.g. login-form discovery tiebreak); it is NOT a route
    to drive actions."""
    live_screenshots: bool = False
    """When true, capture a screenshot of the active browser session after
    every node (success or failure) and emit a `live_screen` event with
    the storage URI. The UI renders the most recent one as a live preview
    panel. Disabled deployments skip the capture entirely — there is no
    cost beyond the boolean check.

    Default off so unit tests that construct LocalExecutor directly
    (without going through Settings) get clean event streams. Production
    wires this through `Settings.live_screenshots` (defaults to true)
    in `AppDependencies`."""

    async def execute(self, dag: Dag, ctx: RunContext) -> RunOutcome:
        env: dict[str, dict[str, Any]] = {}
        alias_to_id = self._alias_index(dag)
        layers = list(topological_layers(dag))
        logger.info(
            "execute run_id=%s nodes=%d layers=%d",
            ctx.run_id,
            len(dag.nodes),
            len(layers),
        )
        try:
            for layer_idx, layer in enumerate(layers):
                logger.debug(
                    "run_id=%s layer %d/%d nodes=%s",
                    ctx.run_id,
                    layer_idx + 1,
                    len(layers),
                    [n.id for n in layer],
                )
                results = await asyncio.gather(
                    *[
                        self._run_node(node, env=env, alias_to_id=alias_to_id, ctx=ctx)
                        for node in layer
                    ],
                    return_exceptions=False,
                )
                for node, outputs in zip(layer, results):
                    env[node.id] = outputs
            logger.info("execute run_id=%s succeeded", ctx.run_id)
            return RunOutcome(run_id=ctx.run_id, status="succeeded", outputs=env)
        except Exception as e:
            logger.exception("execute run_id=%s failed: %s", ctx.run_id, e)
            await self.signals.cancel_all_for(ctx.run_id)
            return RunOutcome(
                run_id=ctx.run_id,
                status="failed",
                outputs=env,
                error={"type": type(e).__name__, "message": str(e)[:500]},
            )

    # --- internals ---------------------------------------------------------

    @staticmethod
    def _alias_index(dag: Dag) -> dict[str, str]:
        idx: dict[str, str] = {}
        for n in dag.nodes:
            idx[n.id] = n.id
            if n.outputs_as is not None:
                idx[n.outputs_as] = n.id
        return idx

    async def _run_node(
        self,
        node: Node,
        *,
        env: dict[str, dict[str, Any]],
        alias_to_id: dict[str, str],
        ctx: RunContext,
    ) -> dict[str, Any]:
        with node_span(
            self.recorder,
            run_id=ctx.run_id,
            tenant_id=ctx.tenant_id,
            node_id=node.id,
            ref=node.ref,
        ):
            inputs = resolve_inputs(node.inputs, env=env, alias_to_id=alias_to_id)
            inputs = self._merge_grant_defaults(node, inputs, ctx)
            logger.debug(
                "node start run_id=%s node_id=%s ref=%s kind=%s",
                ctx.run_id,
                node.id,
                node.ref,
                node.kind,
            )
            try:
                outputs = await self._dispatch(node, inputs, ctx)
            except Exception as e:
                logger.warning(
                    "node failed run_id=%s node_id=%s ref=%s err=%s: %s",
                    ctx.run_id,
                    node.id,
                    node.ref,
                    type(e).__name__,
                    e,
                )
                raise
            finally:
                # Best-effort live screenshot — runs whether the node
                # succeeded or failed so the UI can show the failure state.
                # Any exception here is swallowed; a broken screenshot must
                # never break a run.
                await self._maybe_emit_live_screen(node, ctx)
            self.recorder.record(
                run_id=ctx.run_id,
                tenant_id=ctx.tenant_id,
                node_id=node.id,
                kind=RunEventKind.NODE_COMPLETED,
                payload={"ref": node.ref, "outputs": _redact_event_outputs(node.ref, outputs)},
            )
            logger.debug(
                "node done  run_id=%s node_id=%s ref=%s output_keys=%s",
                ctx.run_id,
                node.id,
                node.ref,
                list(outputs.keys()) if isinstance(outputs, dict) else type(outputs).__name__,
            )
            return outputs

    @staticmethod
    def _merge_grant_defaults(
        node: Node, inputs: dict[str, Any], ctx: RunContext
    ) -> dict[str, Any]:
        """If a capability node references a granted alias, fill missing
        inputs from the grant's `input_defaults`.

        Per-tenant config like `login_url` belongs on the grant, not in
        the DAG — the planner doesn't know specific URLs and would
        otherwise hallucinate them. The DAG carries the alias; the
        executor injects the URL (and any other site-specific defaults)
        from the grant at run time. Explicit DAG inputs always win.
        """
        if node.kind is not NodeKind.CAPABILITY:
            return inputs
        alias = inputs.get("account_alias")
        if not isinstance(alias, str) or not alias:
            return inputs
        grants = ctx.activity_ctx.granted_capabilities or {}
        per_alias = grants.get(node.ref, {})
        defaults = (per_alias.get(alias) or {}).get("input_defaults") or {}
        if not defaults:
            return inputs
        # Don't overwrite anything the planner explicitly set; only fill
        # holes. Display-only fields like `display_name` aren't part of
        # any capability's input_schema, so they're harmless to copy and
        # get filtered out at the input-validation step anyway.
        merged: dict[str, Any] = dict(inputs)
        for k, v in defaults.items():
            merged.setdefault(k, v)
        return merged

    async def _maybe_emit_live_screen(self, node: Node, ctx: RunContext) -> None:
        """Capture and persist a screenshot of the most recently active
        browser session, then record a LIVE_SCREEN event pointing at it.

        Browser sessions are stashed in `session_state` under keys
        prefixed `browser:` (see `activities/browser.py`). The most
        recently inserted holder is the right session to peek at — for
        the typical single-session run that's the only one; for
        multi-session flows it's the one the just-finished node was
        operating on.
        """
        if not self.live_screenshots:
            return
        actx = ctx.activity_ctx
        state = getattr(actx, "session_state", None) or {}
        holders = [v for k, v in state.items() if str(k).startswith("browser:")]
        if not holders:
            return
        sess = getattr(holders[-1], "session", None)
        if sess is None or not hasattr(sess, "screenshot"):
            return
        store = getattr(actx, "object_store", None)
        if store is None:
            return
        try:
            png = await sess.screenshot()
        except Exception:
            logger.debug("live_screen: screenshot failed for run_id=%s node_id=%s", ctx.run_id, node.id, exc_info=True)
            return
        try:
            key = f"runs/{ctx.run_id}/livescreen/{uuid.uuid4().hex}.png"
            obj = store.put(str(ctx.tenant_id), key, png)
        except Exception:
            logger.debug("live_screen: object_store.put failed", exc_info=True)
            return
        self.recorder.record(
            run_id=ctx.run_id,
            tenant_id=ctx.tenant_id,
            node_id=node.id,
            kind=RunEventKind.LIVE_SCREEN,
            payload={"uri": obj.uri},
        )

    async def _dispatch(
        self, node: Node, inputs: dict[str, Any], ctx: RunContext
    ) -> dict[str, Any]:
        if node.kind is NodeKind.CONTROL:
            return await self._run_control(node, inputs, ctx)
        handler = self.activities.get(node.ref)
        if handler is None:
            raise RuntimeError(f"no activity handler registered for ref {node.ref!r}")
        # Per-call clone so parallel nodes don't clobber `node_id`. The
        # mutable shared bits (session_state, granted_capabilities) are
        # the same dict references — `replace` is shallow.
        per_call_ctx = dataclasses.replace(
            ctx.activity_ctx,
            signals=self.signals,
            node_id=node.id,
            llm=self.llm,
        )
        return await handler(per_call_ctx, inputs)

    async def _run_control(
        self, node: Node, inputs: dict[str, Any], ctx: RunContext
    ) -> dict[str, Any]:
        if node.ref == "control.wait":
            seconds = float(inputs.get("seconds", 0))
            logger.debug("control.wait run_id=%s node_id=%s seconds=%s", ctx.run_id, node.id, seconds)
            await asyncio.sleep(seconds)
            return {}
        if node.ref == "human.prompt":
            message = inputs["message"]
            expects = inputs.get("expects", "text")
            timeout_seconds = int(inputs.get("timeout_seconds", 300))
            logger.info(
                "human.prompt opened run_id=%s node_id=%s expects=%s timeout=%ds",
                ctx.run_id,
                node.id,
                expects,
                timeout_seconds,
            )
            self.recorder.record(
                run_id=ctx.run_id,
                tenant_id=ctx.tenant_id,
                node_id=node.id,
                kind=RunEventKind.RUN_PAUSED,
                payload={"reason": "human_prompt", "message": message, "expects": expects},
            )
            prompt = await self.signals.open(
                run_id=ctx.run_id, node_id=node.id, message=message, expects=expects,
            )
            try:
                response = await asyncio.wait_for(prompt.future, timeout=timeout_seconds)
            except asyncio.TimeoutError as e:
                logger.warning(
                    "human.prompt TIMEOUT run_id=%s node_id=%s after %ds",
                    ctx.run_id,
                    node.id,
                    timeout_seconds,
                )
                raise RuntimeError(
                    f"human.prompt timed out after {timeout_seconds}s on node {node.id}"
                ) from e
            logger.info(
                "human.prompt resolved run_id=%s node_id=%s response_len=%d",
                ctx.run_id,
                node.id,
                len(response),
            )
            self.recorder.record(
                run_id=ctx.run_id,
                tenant_id=ctx.tenant_id,
                node_id=node.id,
                kind=RunEventKind.SIGNAL_RECEIVED,
                payload={"length": len(response)},  # never include the value itself
            )
            self.recorder.record(
                run_id=ctx.run_id,
                tenant_id=ctx.tenant_id,
                node_id=None,
                kind=RunEventKind.RUN_RESUMED,
                payload={},
            )
            return {"response": response}
        raise RuntimeError(f"unknown control ref: {node.ref!r}")


# Refs whose outputs may carry user-typed secrets (OTPs, captcha answers,
# free-form responses) and must NOT be persisted to the event timeline.
# Live `outputs` are still passed downstream via env — only the recorded
# event payload is scrubbed.
_SENSITIVE_OUTPUT_FIELDS: dict[str, set[str]] = {
    "human.prompt": {"response"},
}


def _redact_event_outputs(ref: str, outputs: dict[str, Any]) -> dict[str, Any]:
    sensitive = _SENSITIVE_OUTPUT_FIELDS.get(ref)
    if not sensitive:
        return outputs
    return {k: ("<redacted>" if k in sensitive else v) for k, v in outputs.items()}
