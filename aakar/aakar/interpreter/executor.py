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

    async def execute(self, dag: Dag, ctx: RunContext) -> RunOutcome:
        env: dict[str, dict[str, Any]] = {}
        alias_to_id = self._alias_index(dag)
        try:
            for layer in topological_layers(dag):
                results = await asyncio.gather(
                    *[
                        self._run_node(node, env=env, alias_to_id=alias_to_id, ctx=ctx)
                        for node in layer
                    ],
                    return_exceptions=False,
                )
                for node, outputs in zip(layer, results):
                    env[node.id] = outputs
            return RunOutcome(run_id=ctx.run_id, status="succeeded", outputs=env)
        except Exception as e:
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
            outputs = await self._dispatch(node, inputs, ctx)
            self.recorder.record(
                run_id=ctx.run_id,
                tenant_id=ctx.tenant_id,
                node_id=node.id,
                kind=RunEventKind.NODE_COMPLETED,
                payload={"ref": node.ref, "outputs": _redact_event_outputs(node.ref, outputs)},
            )
            return outputs

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
            ctx.activity_ctx, signals=self.signals, node_id=node.id
        )
        return await handler(per_call_ctx, inputs)

    async def _run_control(
        self, node: Node, inputs: dict[str, Any], ctx: RunContext
    ) -> dict[str, Any]:
        if node.ref == "control.wait":
            seconds = float(inputs.get("seconds", 0))
            await asyncio.sleep(seconds)
            return {}
        if node.ref == "human.prompt":
            message = inputs["message"]
            expects = inputs.get("expects", "text")
            timeout_seconds = int(inputs.get("timeout_seconds", 300))
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
                raise RuntimeError(
                    f"human.prompt timed out after {timeout_seconds}s on node {node.id}"
                ) from e
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
