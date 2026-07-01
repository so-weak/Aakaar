"""LocalExecutor — async, in-process DAG interpreter.

Walks topological layers, dispatches each node to either:
  - a registered activity handler (most refs)
  - a control-node handler (control.wait, human.prompt) the executor knows
    about directly

Within a layer, nodes run concurrently as explicit tasks. On any node
failure — or an operator cancel surfacing as `RunCancelled` from a control
node — in-flight peers are allowed to finish before the error is re-raised
(we don't cancel them mid-flight — they may hold external state like browser
sessions that needs graceful close), but no further layers start. Settling
the whole layer first is what keeps the run's terminal status and session
teardown from landing while a sibling is still executing detached (see
`_run_layer`).

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
from typing import Any, Protocol, cast

from aakaar.db.models import RunEventKind, RunMode
from aakaar.interpreter.activities.registry import ActivityRegistry
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.interpreter.controls import RunCancelled, RunControlHandle
from aakaar.interpreter.durability import CheckpointStore, ResumeState
from aakaar.interpreter.events import EventRecorder, node_span
from aakaar.interpreter.human_tasks import HumanTaskStore
from aakaar.interpreter.refs import resolve_inputs
from aakaar.interpreter.signals import PendingPrompt, SignalHub
from aakaar.interpreter.topology import topological_layers
from aakaar.shared.dag.refs import INPUTS_ALIAS
from aakaar.shared.dag.types import Dag, Node, NodeKind

logger = logging.getLogger(__name__)


# ---------- public types --------------------------------------------------


@dataclass
class RunContext:
    run_id: uuid.UUID
    tenant_id: uuid.UUID
    activity_ctx: ActivityContext
    run_target: str | None = None
    """Run-level placement chosen at launch. When set, it overrides every node's
    own `target` for this run ("server" forces everything local; an agent/pool
    runs the whole workflow there). None falls back to per-node targets. Control
    nodes always run on the server regardless."""
    controls: RunControlHandle | None = None
    """Operator pause/cancel handle, consulted at every layer boundary. None
    for direct executor use (unit tests); the orchestrator always supplies
    one."""
    mode: str = RunMode.LIVE
    """'live' | 'dry_run'. In dry_run the executor walks the full DAG topology
    but short-circuits side-effecting capabilities to a simulated marker instead
    of performing the real effect. Read-only entries still run. Set from
    `runs.mode` by the orchestrator."""
    inputs: dict[str, Any] = field(default_factory=dict)
    """The JSON `inputs` supplied at run start. Seeded into the run env under
    the reserved `inputs` alias so node inputs can reference `${inputs.key}` —
    letting one seeded workflow be re-run forever with different values and no
    planner. Immutable for the run; re-seeded verbatim on resume."""
    resume: ResumeState | None = None
    """When set, the run is being re-driven after a restart from a checkpoint:
    the executor skips every layer before `resume.next_layer_index`, seeds env
    from `resume.env`, and never re-dispatches or re-emits events for the nodes
    in `resume.completed_ids` (the financial-integrity rule). None = fresh run."""


@dataclass
class RunOutcome:
    run_id: uuid.UUID
    status: str  # "succeeded" | "failed" | "cancelled"
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
    remote_dispatcher: Any = None
    """Optional `RemoteDispatcher`. When set, any node whose `target` selects a
    remote agent is executed there instead of by a local handler. Control flow,
    retries, and events stay here on the server; only the node's execution is
    remote. None means single-host execution (the historical behavior)."""
    checkpoints: CheckpointStore | None = None
    """Optional `CheckpointStore`. When set, the executor persists a per-layer
    checkpoint (completed node ids + redacted env snapshot) after each DAG layer
    settles, so a restart can resume mid-DAG. None = no durability (unit tests
    that construct LocalExecutor directly); the run simply re-runs from scratch
    on a restart, the historical behavior."""
    human_tasks: HumanTaskStore | None = None
    """Optional `HumanTaskStore`. When set, a `human.prompt` node also persists a
    durable, SLA-bounded `HumanTask` row alongside the in-process SignalHub, and
    resolves/cancels it as the prompt settles. None = SignalHub-only (unit tests
    and headless flows); the in-memory prompt behavior is unchanged."""

    async def execute(self, dag: Dag, ctx: RunContext) -> RunOutcome:
        env: dict[str, dict[str, Any]] = {}
        resume = ctx.resume
        resume_from = 0
        completed_ids: frozenset[str] = frozenset()
        if resume is not None:
            # Re-driving after a restart: seed the env captured at the last
            # settled layer and skip everything up to the resume boundary. The
            # completed ids are skipped INSIDE the boundary layer too, so a
            # partially-settled layer is finished without re-running its done
            # nodes or re-emitting their events.
            env = {k: dict(v) for k, v in resume.env.items()}
            resume_from = resume.next_layer_index
            completed_ids = resume.completed_ids
        alias_to_id = self._alias_index(dag)
        # Seed the run-level inputs namespace so `${inputs.key}` resolves. It is
        # not a node, so it has no events, never "completes", and is re-seeded
        # verbatim on resume (inputs are immutable for the run).
        alias_to_id[INPUTS_ALIAS] = INPUTS_ALIAS
        env[INPUTS_ALIAS] = dict(ctx.inputs)
        layers = list(topological_layers(dag))
        logger.info(
            "execute run_id=%s nodes=%d layers=%d mode=%s resume_from=%d",
            ctx.run_id,
            len(dag.nodes),
            len(layers),
            ctx.mode,
            resume_from,
        )
        if ctx.mode == RunMode.DRY_RUN:
            self.recorder.record(
                run_id=ctx.run_id,
                tenant_id=ctx.tenant_id,
                node_id=None,
                kind=RunEventKind.LOG,
                payload={"dry_run": True, "message": "executing in dry-run mode; side-effecting nodes are simulated"},
            )
        try:
            for layer_idx, layer in enumerate(layers):
                if layer_idx < resume_from:
                    # Already settled before the restart; its outputs are in the
                    # seeded env. Don't touch it (no events, no dispatch).
                    continue
                if ctx.controls is not None:
                    # Layer-boundary control point: blocks while operator-
                    # paused, raises RunCancelled once a cancel is requested.
                    await ctx.controls.checkpoint()
                # Within the boundary layer, drop nodes already completed before
                # the restart — their outputs are seeded, re-running them would
                # double an irreversible side effect and re-emit their events.
                pending_nodes = [n for n in layer if n.id not in completed_ids]
                logger.debug(
                    "run_id=%s layer %d/%d nodes=%s (skipped_completed=%d)",
                    ctx.run_id,
                    layer_idx + 1,
                    len(layers),
                    [n.id for n in pending_nodes],
                    len(layer) - len(pending_nodes),
                )
                if pending_nodes:
                    results = await self._run_layer(
                        pending_nodes, env=env, alias_to_id=alias_to_id, ctx=ctx
                    )
                    for node, outputs in zip(pending_nodes, results, strict=True):
                        env[node.id] = outputs
                # Checkpoint the boundary: completed = every node in this layer
                # whose output is now in env (seeded + just-run). After the first
                # resumed layer settles, completed_ids no longer applies.
                self._save_checkpoint(ctx, layer_idx, layer, env)
                completed_ids = frozenset()
            logger.info("execute run_id=%s succeeded", ctx.run_id)
            return RunOutcome(run_id=ctx.run_id, status="succeeded", outputs=env)
        except RunCancelled:
            logger.info("execute run_id=%s cancelled", ctx.run_id)
            await self.signals.cancel_all_for(ctx.run_id)
            return RunOutcome(run_id=ctx.run_id, status="cancelled", outputs=env)
        except Exception as e:
            logger.exception("execute run_id=%s failed: %s", ctx.run_id, e)
            await self.signals.cancel_all_for(ctx.run_id)
            error: dict[str, Any] = {"type": type(e).__name__, "message": str(e)[:500]}
            # Surface which node/ref failed (tagged in `_run_node`) so the run
            # row carries "Failed at step: <node>" for the UI/timeline.
            step = getattr(e, "aakaar_failed_node_id", None)
            ref = getattr(e, "aakaar_failed_node_ref", None)
            if step is not None:
                error["step"] = step
            if ref is not None:
                error["ref"] = ref
            return RunOutcome(
                run_id=ctx.run_id,
                status="failed",
                outputs=env,
                error=error,
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

    def _save_checkpoint(
        self,
        ctx: RunContext,
        layer_index: int,
        layer: list[Node],
        env: dict[str, dict[str, Any]],
    ) -> None:
        """Persist the layer-boundary checkpoint, never breaking the run.

        `completed_node_ids` is every node in this layer whose output is now in
        env (the just-run nodes plus, on a resumed boundary, the seeded ones).
        A checkpoint failure is logged and swallowed: durability is best-effort
        and must not turn a healthy run into a failure. The store itself redacts
        the env before it lands.
        """
        if self.checkpoints is None:
            return
        completed = [n.id for n in layer if n.id in env]
        try:
            self.checkpoints.save_layer(
                run_id=ctx.run_id,
                tenant_id=ctx.tenant_id,
                layer_index=layer_index,
                completed_node_ids=completed,
                env=env,
            )
        except Exception:
            logger.warning(
                "checkpoint save failed run_id=%s layer=%d; continuing without it",
                ctx.run_id,
                layer_index,
                exc_info=True,
            )

    def _cancel_human_task(self, ctx: RunContext, node_id: str) -> None:
        """Mark the durable HumanTask CANCELLED when its prompt unwinds (cancel
        or timeout). The store swallows its own errors; this is a thin guard so
        callers don't repeat the None check."""
        if self.human_tasks is not None:
            self.human_tasks.cancel(run_id=ctx.run_id, node_id=node_id)

    async def _run_layer(
        self,
        layer: list[Node],
        *,
        env: dict[str, dict[str, Any]],
        alias_to_id: dict[str, str],
        ctx: RunContext,
    ) -> list[dict[str, Any]]:
        """Run a layer's nodes concurrently, settling all siblings before
        surfacing any node's error.

        A control node (human.prompt / control.wait) raising RunCancelled, or
        any node failing, must not leave its in-flight peers running detached:
        the orchestrator would stamp the run terminal and tear down shared
        session_state (browser sessions, etc.) while those coroutines are still
        touching it, and their late events would land after run_cancelled.

        So we drive the layer as explicit tasks and `asyncio.wait` for ALL of
        them to finish, even after the first failure. Peers are NOT cancelled —
        an in-flight node may hold external state that needs a graceful close
        (the same contract as the historical gather). Once everything has
        settled we re-raise, preferring RunCancelled (operator intent) over an
        incidental node failure, then the first failure otherwise.
        """
        tasks = [
            asyncio.ensure_future(
                self._run_node(node, env=env, alias_to_id=alias_to_id, ctx=ctx)
            )
            for node in layer
        ]
        await asyncio.wait(tasks)
        cancelled: BaseException | None = None
        failure: BaseException | None = None
        for task in tasks:
            if task.cancelled():
                # A node task was hard-cancelled (not the cooperative
                # RunCancelled path). `task.exception()` would re-raise here;
                # treat it as a cancellation so the whole layer still settles
                # and the run unwinds CANCELLED rather than leaking a bare
                # CancelledError with a misleading status.
                if cancelled is None:
                    cancelled = RunCancelled("a node task was cancelled mid-layer")
                continue
            exc = task.exception()
            if exc is None:
                continue
            if isinstance(exc, RunCancelled):
                if cancelled is None:
                    cancelled = exc
            elif failure is None:
                failure = exc
        if cancelled is not None:
            raise cancelled
        if failure is not None:
            raise failure
        return [task.result() for task in tasks]

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
                outputs = await self._dispatch_with_retry(node, inputs, ctx)
            except Exception as e:
                logger.warning(
                    "node failed run_id=%s node_id=%s ref=%s err=%s: %s",
                    ctx.run_id,
                    node.id,
                    node.ref,
                    type(e).__name__,
                    e,
                )
                # Tag the failing node/ref onto the exception so the run's
                # terminal error can name the step ("Failed at step: login")
                # without the caller scanning the event log. Guarded so an
                # outer re-raise never overwrites the innermost failing node.
                if not hasattr(e, "aakaar_failed_node_id"):
                    e.aakaar_failed_node_id = node.id  # type: ignore[attr-defined]
                    e.aakaar_failed_node_ref = node.ref  # type: ignore[attr-defined]
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
    def _is_side_effecting(node: Node, ctx: RunContext) -> bool:
        """Whether this node performs an external, irreversible side effect.

        Reads the `side_effecting` flag off the node's registry definition.
        Tri-state -> bool for the dry-run gate: True and None (UNDECLARED) both
        count as side-effecting so the simulation never performs a real effect
        for a capability that forgot to declare; only an explicit False is
        treated as read-only. A ref with no registry definition (shouldn't
        happen post-validation) is treated conservatively as side-effecting.
        """
        registry = getattr(ctx.activity_ctx, "registry", None)
        if registry is None:
            return True
        defn = registry.get(node.ref)
        if defn is None:
            return True
        return defn.side_effecting is not False

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
        # Fill holes from the grant. A "hole" means: key missing OR value
        # is None or "". The planner sometimes emits `login_url: null`
        # explicitly (LLMs are eager to include every declared field even
        # when they don't know the value); treating those nulls as "set"
        # would override the per-tenant default and surface as a runtime
        # error. Real overrides (a non-empty value the planner deliberately
        # chose) still win. Display-only fields like `display_name` aren't
        # part of any capability's input_schema, so they're harmless to
        # copy and get filtered out at the input-validation step anyway.
        merged: dict[str, Any] = dict(inputs)
        for k, v in defaults.items():
            existing = merged.get(k)
            if existing is None or existing == "":
                merged[k] = v
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

    async def _dispatch_with_retry(
        self, node: Node, inputs: dict[str, Any], ctx: RunContext
    ) -> dict[str, Any]:
        """Dispatch a node, retrying on failure per its optional retry policy.

        Control nodes are never retried (a human-prompt timeout or a wait is
        not a transient fault). Each retry emits a NODE_RETRYING event so the
        timeline reflects the attempt; the final failure propagates unchanged.
        """
        retry = node.retry if node.kind is not NodeKind.CONTROL else None
        max_attempts = retry.max_attempts if retry else 1
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self._dispatch(node, inputs, ctx)
            except Exception as e:
                if attempt >= max_attempts:
                    raise
                backoff = (retry.backoff_ms / 1000.0) * attempt if retry else 0.0
                logger.warning(
                    "node retry run_id=%s node_id=%s ref=%s attempt=%d/%d backoff=%.2fs err=%s",
                    ctx.run_id,
                    node.id,
                    node.ref,
                    attempt,
                    max_attempts,
                    backoff,
                    type(e).__name__,
                )
                self.recorder.record(
                    run_id=ctx.run_id,
                    tenant_id=ctx.tenant_id,
                    node_id=node.id,
                    kind=RunEventKind.NODE_RETRYING,
                    payload={
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "error": {"type": type(e).__name__, "message": str(e)[:300]},
                    },
                )
                if backoff > 0:
                    await asyncio.sleep(backoff)

    async def _dispatch(
        self, node: Node, inputs: dict[str, Any], ctx: RunContext
    ) -> dict[str, Any]:
        if node.kind is NodeKind.CONTROL:
            return await self._run_control(node, inputs, ctx)
        # Dry-run short-circuit: in a simulation, a side-effecting entry must not
        # perform its real effect (no SMTP/SFTP/HTTP-POST/desktop/file write).
        # Undeclared (`side_effecting is None`) is treated as side-effecting so a
        # capability that forgot to declare can never move money in a dry-run.
        # Read-only entries (`side_effecting is False`) run for real even here.
        if ctx.mode == RunMode.DRY_RUN and self._is_side_effecting(node, ctx):
            logger.info(
                "dry-run: simulating side-effecting node run_id=%s node_id=%s ref=%s",
                ctx.run_id,
                node.id,
                node.ref,
            )
            self.recorder.record(
                run_id=ctx.run_id,
                tenant_id=ctx.tenant_id,
                node_id=node.id,
                kind=RunEventKind.LOG,
                payload={"dry_run": True, "simulated": True, "would_run": node.ref},
            )
            return {"simulated": True, "would_run": node.ref}
        # Effective placement: a run-level target (chosen at launch) overrides
        # each node's own target for the whole run; otherwise the node's own
        # target applies. Control nodes (handled above) always stay on the server.
        effective_target = ctx.run_target if ctx.run_target is not None else node.target
        # Remote placement: ship the node to a selected agent. The agent owns
        # the handler implementation, so no local handler is required.
        if self.remote_dispatcher is not None and effective_target not in (None, "server"):
            per_call_ctx = dataclasses.replace(
                ctx.activity_ctx, signals=self.signals, node_id=node.id, llm=self.llm
            )
            return cast(
                "dict[str, Any]",
                await self.remote_dispatcher.run(
                    node, inputs, per_call_ctx, target=effective_target
                ),
            )
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
            if ctx.controls is None:
                await asyncio.sleep(seconds)
                return {}
            # Sleep, but wake early on an operator cancel so a long wait
            # can't hold the run open past a cancel request.
            try:
                await asyncio.wait_for(ctx.controls.cancel_event.wait(), timeout=seconds)
            except TimeoutError:
                return {}
            raise RunCancelled(f"run cancelled during control.wait node {node.id}")
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
            # Durable, SLA-bounded shadow of the in-memory prompt. Best-effort:
            # the SignalHub remains the thing the coroutine actually awaits.
            if self.human_tasks is not None:
                self.human_tasks.open(
                    run_id=ctx.run_id,
                    tenant_id=ctx.tenant_id,
                    node_id=node.id,
                    message=message,
                    expects=expects,
                    timeout_seconds=timeout_seconds,
                )
            try:
                response = await self._await_prompt(prompt, timeout_seconds, ctx, node.id)
            except RunCancelled:
                # cancel_all_for (orchestrator) or _await_prompt's own race
                # already removed/cancelled the prompt; surface cooperatively.
                self._cancel_human_task(ctx, node.id)
                raise
            except asyncio.CancelledError:
                if ctx.controls is not None and ctx.controls.cancel_requested:
                    # The orchestrator cancelled the prompt future as part
                    # of an operator cancel — unwind cooperatively rather
                    # than letting CancelledError kill the drive task.
                    self._cancel_human_task(ctx, node.id)
                    raise RunCancelled(
                        f"run cancelled while waiting on human.prompt node {node.id}"
                    ) from None
                raise
            except TimeoutError as e:
                logger.warning(
                    "human.prompt TIMEOUT run_id=%s node_id=%s after %ds",
                    ctx.run_id,
                    node.id,
                    timeout_seconds,
                )
                self._cancel_human_task(ctx, node.id)
                raise RuntimeError(
                    f"human.prompt timed out after {timeout_seconds}s on node {node.id}"
                ) from e
            logger.info(
                "human.prompt resolved run_id=%s node_id=%s response_len=%d",
                ctx.run_id,
                node.id,
                len(response),
            )
            if self.human_tasks is not None:
                # The responder identity isn't threaded through the SignalHub
                # response; the task records the answer (redacted for OTPs)
                # without it. None = answered via the in-process resolve path.
                self.human_tasks.resolve(
                    run_id=ctx.run_id, node_id=node.id, response=response
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
                payload={"reason": "human_prompt"},
            )
            return {"response": response}
        raise RuntimeError(f"unknown control ref: {node.ref!r}")

    async def _await_prompt(
        self,
        prompt: PendingPrompt,
        timeout_seconds: int,
        ctx: RunContext,
        node_id: str,
    ) -> str:
        """Wait for a human.prompt response, racing the response future against
        an operator cancel and the per-node timeout.

        Why race rather than just `wait_for(prompt.future, ...)`: a cancel that
        lands in the window between the layer checkpoint and this `await`
        (cancel_all_for ran before the prompt registered, so it popped nothing)
        would otherwise leave the future un-cancelled and the run would sit out
        the full timeout, then unwind via the TimeoutError->RuntimeError path
        and finish FAILED — never CANCELLED. Watching cancel_event directly
        closes that window: a cancel requested at ANY point wins the race.
        """
        controls = ctx.controls
        if controls is None:
            # No operator handle (direct executor use / unit tests): the only
            # interrupt is the prompt timeout, same as the historical path.
            try:
                return await asyncio.wait_for(prompt.future, timeout=timeout_seconds)
            except asyncio.CancelledError:
                raise  # a real task cancel, not an operator cancel
        if controls.cancel_requested:
            # Cancel already requested before we got here — don't even start
            # waiting. Drop the just-registered prompt so /respond 409s.
            await self.signals.cancel_all_for(ctx.run_id)
            raise RunCancelled(
                f"run cancelled while waiting on human.prompt node {node_id}"
            )
        cancel_wait = asyncio.ensure_future(controls.cancel_event.wait())
        # Heterogeneous result types (str response vs bool cancel flag); the
        # results are read off the individual futures, never the wait() set.
        waitable: set[asyncio.Future[Any]] = {prompt.future, cancel_wait}
        try:
            done, _pending = await asyncio.wait(
                waitable,
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if controls.cancel_requested:
                await self.signals.cancel_all_for(ctx.run_id)
                raise RunCancelled(
                    f"run cancelled while waiting on human.prompt node {node_id}"
                )
            if prompt.future in done:
                # set_result-or-cancel: resolve() may have cancelled it (a
                # racing cancel_all_for) — surface that as a cooperative cancel.
                if prompt.future.cancelled():
                    raise RunCancelled(
                        f"run cancelled while waiting on human.prompt node {node_id}"
                    )
                return prompt.future.result()
            # Neither cancel nor a response: the timeout elapsed.
            raise TimeoutError
        finally:
            cancel_wait.cancel()


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
