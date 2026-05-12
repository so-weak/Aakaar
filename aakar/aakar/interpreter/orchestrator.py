"""RunOrchestrator — drive runs to completion.

Layering:
  - The API endpoint owns the "create the run row + collect grants + load
    the DAG" work (uses repositories).
  - The orchestrator owns the "execute and persist final status" work
    (uses SessionFactory directly for status updates).

This keeps the interpreter package free of imports from `aakar.api`.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aakar.db.models import Run, RunStatus
from aakar.db.session import SessionFactory
from aakar.interpreter.activities.types import ActivityContext
from aakar.interpreter.events import EventRecorder
from aakar.interpreter.executor import Executor, RunContext, RunOutcome
from aakar.interpreter.signals import SignalHub
from aakar.shared.dag.types import Dag
from aakar.shared.registry import Registry
from aakar.storage.object_store import ObjectStorage
from aakar.vault import Vault


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
    ) -> asyncio.Task[RunOutcome]:
        logger.info(
            "schedule run_id=%s tenant_id=%s nodes=%d granted_caps=%d",
            run_id,
            tenant_id,
            len(dag.nodes),
            len(granted_caps),
        )
        task = asyncio.create_task(
            self._drive(
                run_id=run_id, tenant_id=tenant_id, dag=dag, granted_caps=granted_caps,
            )
        )
        self._tasks[run_id] = task
        return task

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
        ctx = RunContext(run_id=run_id, tenant_id=tenant_id, activity_ctx=activity_ctx)
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
                run.ended_at = datetime.now(timezone.utc)
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
