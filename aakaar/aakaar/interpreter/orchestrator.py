"""RunOrchestrator — drive runs to completion.

Layering:
  - The API endpoint owns the "create the run row + collect grants + load
    the DAG" work (uses repositories).
  - The orchestrator owns the "execute and persist final status" work
    (uses SessionFactory directly for status updates).

This keeps the interpreter package free of imports from `aakaar.api`.

Public surface:
  - schedule(run_id, tenant_id, dag, granted_caps) — kicks off an
    asyncio task to drive the run; returns immediately
  - respond(run_id, node_id, response) — resolves a paused human.prompt
  - wait_for(run_id) — test helper: await the asyncio task
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aakaar.db.models import Run, RunStatus
from aakaar.db.session import SessionFactory
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.interpreter.events import EventRecorder
from aakaar.interpreter.executor import Executor, RunContext, RunOutcome
from aakaar.interpreter.signals import SignalHub
from aakaar.shared.dag.types import Dag
from aakaar.shared.registry import Registry
from aakaar.storage.object_store import ObjectStorage
from aakaar.vault import Vault

logger = logging.getLogger(__name__)


@dataclass
class RunOrchestrator:
    session_factory: SessionFactory
    executor: Executor
    signals: SignalHub
    recorder: EventRecorder
    registry: Registry
    object_store: ObjectStorage
    vault: Vault
    browser_pool: Any = None
    download_mirror_dir: Path | None = None
    _tasks: dict[uuid.UUID, asyncio.Task[RunOutcome]] = field(default_factory=dict)

    def schedule(
        self,
        *,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        dag: Dag,
        granted_caps: dict[str, dict[str, Any]],
        run_target: str | None = None,
    ) -> asyncio.Task[RunOutcome]:
        logger.info(
            "schedule run_id=%s tenant_id=%s nodes=%d granted_caps=%d run_target=%s",
            run_id,
            tenant_id,
            len(dag.nodes),
            len(granted_caps),
            run_target,
        )
        task = asyncio.create_task(
            self._drive(
                run_id=run_id,
                tenant_id=tenant_id,
                dag=dag,
                granted_caps=granted_caps,
                run_target=run_target,
            )
        )
        self._tasks[run_id] = task
        return task

    def recover_interrupted_runs(self) -> int:
        """Reconcile runs left mid-flight by a crashed/restarted process.

        The in-process LocalExecutor holds run state in memory, so a restart
        loses any QUEUED/RUNNING run. Rather than leave them as permanent
        zombies, mark them FAILED with a clear reason on startup — the UI then
        shows a definitive terminal status and the user can re-run. (A durable
        re-attach would require a Temporal-style executor, which is out of
        scope.) Called once from the app lifespan startup hook.
        """
        from sqlalchemy import select

        recovered = 0
        with self.session_factory.session() as s:
            rows = (
                s.execute(
                    select(Run).where(
                        Run.status.in_([RunStatus.QUEUED, RunStatus.RUNNING])
                    )
                )
                .scalars()
                .all()
            )
            for run in rows:
                run.status = RunStatus.FAILED
                run.error = {
                    "type": "Interrupted",
                    "message": "Run interrupted by a server restart and could not be resumed.",
                }
                run.ended_at = datetime.now(UTC)
                recovered += 1
                try:
                    self.recorder.record(
                        run_id=run.id,
                        tenant_id=run.tenant_id,
                        node_id=None,
                        kind="node_failed",
                        payload={"reason": "interrupted_by_restart"},
                    )
                except Exception:
                    logger.debug("recovery: event record failed for run %s", run.id, exc_info=True)
            if recovered:
                s.commit()
        if recovered:
            logger.warning("recovered %d interrupted run(s) -> FAILED on startup", recovered)
        return recovered

    async def respond(self, *, run_id: uuid.UUID, node_id: str, response: str) -> None:
        logger.info("respond run_id=%s node_id=%s", run_id, node_id)
        await self.signals.resolve(run_id, node_id, response)

    async def wait_for(self, run_id: uuid.UUID) -> RunOutcome:
        task = self._tasks.get(run_id)
        if task is None:
            raise KeyError(run_id)
        return await task

    # --- internals ---------------------------------------------------------

    async def _drive(
        self,
        *,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        dag: Dag,
        granted_caps: dict[str, dict[str, Any]],
        run_target: str | None = None,
    ) -> RunOutcome:
        logger.debug("_drive: marking run %s as RUNNING", run_id)
        self._update_status(run_id=run_id, status=RunStatus.RUNNING)

        activity_ctx = ActivityContext(
            tenant_id=tenant_id,
            run_id=run_id,
            registry=self.registry,
            object_store=self.object_store,
            vault=self.vault,
            granted_capabilities=granted_caps,
            browser_pool=self.browser_pool,
            download_mirror_dir=self.download_mirror_dir,
        )
        ctx = RunContext(
            run_id=run_id,
            tenant_id=tenant_id,
            activity_ctx=activity_ctx,
            run_target=run_target,
        )
        try:
            outcome = await self.executor.execute(dag, ctx)
        except Exception as e:
            logger.exception("orchestrator caught uncaught exception in run %s", run_id)
            outcome = RunOutcome(
                run_id=run_id,
                status="failed",
                error={"type": type(e).__name__, "message": str(e)[:500]},
            )

        # Best-effort cleanup of any live handles (browser sessions etc.).
        for cleanup in list(activity_ctx.session_state.values()):
            close = getattr(cleanup, "close", None)
            if callable(close):
                try:
                    res = close()
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    logger.exception("session_state cleanup failed for run %s", run_id)

        final_status = (
            RunStatus.SUCCEEDED if outcome.status == "succeeded" else RunStatus.FAILED
        )
        if final_status == RunStatus.SUCCEEDED:
            logger.info("run %s SUCCEEDED (outputs=%d nodes)", run_id, len(outcome.outputs))
        else:
            logger.warning(
                "run %s FAILED error=%s",
                run_id,
                (outcome.error or {}).get("message", outcome.error),
            )
        self._update_status(
            run_id=run_id,
            status=final_status,
            outputs=_redact_outputs(outcome.outputs),
            error=outcome.error,
            end=True,
        )
        return outcome

    def _update_status(
        self,
        *,
        run_id: uuid.UUID,
        status: str,
        outputs: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        end: bool = False,
    ) -> None:
        with self.session_factory.session() as s:
            run = s.get(Run, run_id)
            if run is None:
                logger.warning("run %s missing from DB during status update", run_id)
                return
            run.status = status
            if outputs is not None:
                run.outputs = outputs
            if error is not None:
                run.error = error
            if end:
                run.ended_at = datetime.now(UTC)
            s.commit()


_REDACT_KEYS = {"password", "token", "api_key", "secret", "authorization"}


def _redact_outputs(env: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Belt-and-suspenders redaction before persisting outputs to the DB."""
    out: dict[str, dict[str, Any]] = {}
    for nid, payload in env.items():
        if not isinstance(payload, dict):
            out[nid] = {"value": "<non-dict output>"}
            continue
        out[nid] = {
            k: ("<redacted>" if k.lower() in _REDACT_KEYS else v) for k, v in payload.items()
        }
    return out
