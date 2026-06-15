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
  - pause_run / resume_run / cancel_run — operator lifecycle controls,
    backed by a per-run ControlHub handle the executor checks between
    DAG layers (see controls.py for the pause-precedence rule)
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

from aakaar.db.models import Run, RunEventKind, RunMode, RunStatus
from aakaar.db.session import SessionFactory
from aakaar.db.tenancy import system_scope, tenant_scope
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.interpreter.controls import ControlHub, RunControlConflict
from aakaar.interpreter.durability import (
    EVENT_RUN_RESUMED_FROM_CHECKPOINT,
    CheckpointStore,
    ResumeState,
)
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
    controls: ControlHub = field(default_factory=ControlHub)
    checkpoints: CheckpointStore | None = None
    """Resume-state loader for crash recovery. When set, `recover_interrupted_runs`
    re-drives a RUNNING run that has a checkpoint instead of failing it. None
    (legacy / minimal tests) keeps the old fail-everything behavior."""
    max_resumes: int = 5
    """Cap on how many times one run may be resumed from a checkpoint across
    restarts, so a poison run that always crashes the same layer eventually
    fails instead of resuming forever (bounded by `runs.resume_count`)."""
    _tasks: dict[uuid.UUID, asyncio.Task[RunOutcome]] = field(default_factory=dict)

    def schedule(
        self,
        *,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        dag: Dag,
        granted_caps: dict[str, dict[str, Any]],
        run_target: str | None = None,
        resume: ResumeState | None = None,
    ) -> asyncio.Task[RunOutcome]:
        logger.info(
            "schedule run_id=%s tenant_id=%s nodes=%d granted_caps=%d run_target=%s resume=%s",
            run_id,
            tenant_id,
            len(dag.nodes),
            len(granted_caps),
            run_target,
            resume.next_layer_index if resume is not None else None,
        )
        self.controls.register(run_id, tenant_id)
        task = asyncio.create_task(
            self._drive(
                run_id=run_id,
                tenant_id=tenant_id,
                dag=dag,
                granted_caps=granted_caps,
                run_target=run_target,
                resume=resume,
            )
        )
        self._tasks[run_id] = task
        return task

    def recover_interrupted_runs(self) -> int:
        """Reconcile runs left mid-flight by a crashed/restarted process.

        The in-process LocalExecutor holds run state in memory, so a restart
        loses the in-flight state of any QUEUED/RUNNING/PAUSED run. Two outcomes:

          - A RUNNING run that HAS a checkpoint (and a `CheckpointStore` is
            wired, and it hasn't already exhausted `max_resumes`) is RESUMED:
            re-scheduled from the next un-settled layer, seeding the checkpoint's
            env and skipping the nodes already completed. Their events are NOT
            re-emitted — re-running an already-done node could double an
            irreversible side effect (the financial-integrity rule).

          - Everything else (no checkpoint, QUEUED before any layer settled,
            PAUSED — whose in-process gate is gone — or a run that has hit the
            resume cap) is marked FAILED with a clear reason, so the UI shows a
            definitive terminal status instead of a perpetual zombie.

        Returns the count of runs reconciled (resumed + failed). Called once
        from the app lifespan startup hook, inside the running event loop, so
        the resume path's `schedule()` (which spawns a task) has a loop.
        """
        from sqlalchemy import select

        resumable: list[uuid.UUID] = []
        reconciled = 0
        # Startup scan spans every tenant's runs — trusted cross-tenant work.
        # PAUSED is included: an operator pause is held by an in-process gate
        # that does not survive a restart, so it can't be resumed as paused.
        with system_scope(), self.session_factory.session() as s:
            rows = (
                s.execute(
                    select(Run).where(
                        Run.status.in_(
                            [RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.PAUSED]
                        )
                    )
                )
                .scalars()
                .all()
            )
            for run in rows:
                if self._can_resume(run):
                    # Defer the actual reschedule until after this read txn
                    # commits — schedule() must not run inside the session.
                    resumable.append(run.id)
                    reconciled += 1
                    continue
                run.status = RunStatus.FAILED
                run.error = {
                    "type": "Interrupted",
                    "message": "Run interrupted by a server restart and could not be resumed.",
                }
                run.ended_at = datetime.now(UTC)
                reconciled += 1
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
            s.commit()
        failed = reconciled - len(resumable)
        if failed:
            logger.warning("recovered %d interrupted run(s) -> FAILED on startup", failed)
        for run_id in resumable:
            self._resume_run(run_id)
        return reconciled

    def _can_resume(self, run: Run) -> bool:
        """Whether a non-terminal run should be re-driven from a checkpoint
        rather than failed. Requires a wired CheckpointStore, a RUNNING status
        (a QUEUED run never settled a layer; a PAUSED run's gate is gone), a
        persisted checkpoint, and resume headroom under `max_resumes`."""
        if self.checkpoints is None:
            return False
        if run.status != RunStatus.RUNNING:
            return False
        if (run.resume_count or 0) >= self.max_resumes:
            logger.warning(
                "run %s hit resume cap (%d); failing instead of resuming",
                run.id,
                self.max_resumes,
            )
            return False
        return run.checkpoint is not None or self.checkpoints.load_resume_state(run.id) is not None

    def _resume_run(self, run_id: uuid.UUID) -> None:
        """Reload a checkpointed run's DAG + grants and re-schedule it from the
        resume boundary. Failures here downgrade the run to FAILED so a run can
        never get stuck un-resumed and un-failed."""
        assert self.checkpoints is not None  # guarded by _can_resume
        try:
            resume = self.checkpoints.load_resume_state(run_id)
            if resume is None:
                raise RuntimeError("resume state vanished between scan and reschedule")
            with system_scope(), self.session_factory.session() as s:
                run = s.get(Run, run_id)
                if run is None:
                    return
                tenant_id = run.tenant_id
                dag = self._load_dag(s, run)
                granted_caps = self._snapshot_grants(s, tenant_id)
                run.resume_count = (run.resume_count or 0) + 1
                s.commit()
            self.recorder.record(
                run_id=run_id,
                tenant_id=tenant_id,
                node_id=None,
                kind=EVENT_RUN_RESUMED_FROM_CHECKPOINT,
                payload={
                    "from_layer": resume.next_layer_index,
                    "skipped_nodes": len(resume.completed_ids),
                },
            )
            logger.warning(
                "resuming run %s from layer %d (skipping %d completed nodes)",
                run_id,
                resume.next_layer_index,
                len(resume.completed_ids),
            )
            # run_target intentionally None on resume: per-node targets still
            # apply; the original run-level placement isn't persisted.
            self.schedule(
                run_id=run_id,
                tenant_id=tenant_id,
                dag=dag,
                granted_caps=granted_caps,
                resume=resume,
            )
        except Exception:
            logger.exception("resume failed for run %s; marking FAILED", run_id)
            self._update_status(
                run_id=run_id,
                status=RunStatus.FAILED,
                error={
                    "type": "ResumeFailed",
                    "message": "Run could not be resumed after restart.",
                },
                end=True,
            )

    @staticmethod
    def _load_dag(s: Any, run: Run) -> Dag:
        """Load the run's pinned WorkflowVersion DAG. Reads db.models directly to
        keep the interpreter package free of `aakaar.api` imports."""
        from sqlalchemy import select

        from aakaar.db.models import WorkflowVersion

        wfv = s.scalar(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == run.workflow_id,
                WorkflowVersion.version == run.workflow_version,
            )
        )
        if wfv is None:
            raise RuntimeError(
                f"workflow version {run.workflow_id}/{run.workflow_version} missing"
            )
        return Dag.model_validate(wfv.dag)

    @staticmethod
    def _snapshot_grants(
        s: Any, tenant_id: uuid.UUID
    ) -> dict[str, dict[str, Any]]:
        """Re-snapshot enabled capability grants for the tenant on resume.

        Mirrors the router's `_snapshot_grants` but reads the model directly
        (no `aakaar.api` import from the interpreter layer). A grant disabled
        while the run was down is correctly excluded from the resumed run."""
        from sqlalchemy import select

        from aakaar.db.models import CapabilityGrant

        granted: dict[str, dict[str, Any]] = {}
        rows = s.scalars(
            select(CapabilityGrant).where(CapabilityGrant.tenant_id == tenant_id)
        )
        for g in rows:
            if g.enabled:
                granted.setdefault(g.capability_ref, {})[g.account_alias] = {
                    "vault_ref": g.vault_ref,
                    "input_defaults": dict(g.input_defaults or {}),
                }
        return granted

    async def respond(self, *, run_id: uuid.UUID, node_id: str, response: str) -> None:
        logger.info("respond run_id=%s node_id=%s", run_id, node_id)
        await self.signals.resolve(run_id, node_id, response)

    def pause_run(self, *, run_id: uuid.UUID) -> None:
        """Hold the run before its next DAG layer (operator pause).

        In-flight nodes finish; the executor then blocks at the layer
        boundary until `resume_run` (or a cancel). Persists PAUSED and
        records a RUN_PAUSED event with reason "operator" so the timeline
        distinguishes it from a human-prompt pause.

        Raises RunNotActive when the run has no live handle here, and
        RunControlConflict when it is already paused or being cancelled.
        No awaits between the checks and the writes, so this can't
        interleave with the drive task's own status updates.
        """
        handle = self.controls.get(run_id)
        if handle.cancel_requested:
            raise RunControlConflict("run is being cancelled")
        if handle.paused:
            raise RunControlConflict("run is already paused")
        handle.pause()
        self._update_status(run_id=run_id, status=RunStatus.PAUSED)
        self.recorder.record(
            run_id=run_id,
            tenant_id=handle.tenant_id,
            node_id=None,
            kind=RunEventKind.RUN_PAUSED,
            payload={"reason": "operator"},
        )
        logger.info("pause_run run_id=%s", run_id)

    def resume_run(self, *, run_id: uuid.UUID) -> None:
        """Release an operator pause so the next DAG layer can start.

        Never resolves a pending human prompt: a run blocked on
        human.prompt is not operator-paused, so resuming it is a conflict
        (the precedence rule lives in the controls module docstring).
        """
        handle = self.controls.get(run_id)
        if not handle.paused:
            if self.signals.list_pending(run_id):
                raise RunControlConflict(
                    "run is waiting on a human prompt; respond to it instead of resuming"
                )
            raise RunControlConflict("run is not paused")
        handle.resume()
        self._update_status(run_id=run_id, status=RunStatus.RUNNING)
        self.recorder.record(
            run_id=run_id,
            tenant_id=handle.tenant_id,
            node_id=None,
            kind=RunEventKind.RUN_RESUMED,
            payload={"reason": "operator"},
        )
        logger.info("resume_run run_id=%s", run_id)

    async def cancel_run(self, *, run_id: uuid.UUID) -> None:
        """Request cooperative cancellation; idempotent.

        In-flight nodes finish; the executor raises at the next layer
        boundary (long control.wait sleeps and human.prompt waits are
        interrupted immediately). The drive task persists the terminal
        CANCELLED status and records the RUN_CANCELLED event as it
        unwinds, so the status may read running/paused for a moment after
        this returns. Raises RunNotActive when the run is not live here.
        """
        handle = self.controls.get(run_id)
        if handle.cancel_requested:
            return
        handle.request_cancel()
        # Interrupt any human.prompt waits so the executor can unwind
        # without sitting out the prompt timeout.
        await self.signals.cancel_all_for(run_id)
        logger.info("cancel_run run_id=%s", run_id)

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
        resume: ResumeState | None = None,
    ) -> RunOutcome:
        # Pin every app-DB write this run performs (status, events) to its
        # tenant, so RLS — when enforcing — keeps a run from touching another
        # tenant's rows. contextvars propagate into the executor's awaited work
        # and any sub-tasks it spawns.
        try:
            with tenant_scope(tenant_id):
                return await self._drive_impl(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    dag=dag,
                    granted_caps=granted_caps,
                    run_target=run_target,
                    resume=resume,
                )
        finally:
            # The control handle is only meaningful while the run is live.
            self.controls.discard(run_id)

    async def _drive_impl(
        self,
        *,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        dag: Dag,
        granted_caps: dict[str, dict[str, Any]],
        run_target: str | None = None,
        resume: ResumeState | None = None,
    ) -> RunOutcome:
        handle = self.controls.peek(run_id)
        if handle is None or not handle.paused:
            # Skip when an operator paused the run before the task got its
            # first slice — the row already says PAUSED and the executor's
            # first checkpoint will hold it; writing RUNNING here would lie.
            logger.debug("_drive: marking run %s as RUNNING", run_id)
            self._update_status(run_id=run_id, status=RunStatus.RUNNING)

        mode = self._load_mode(run_id)
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
            controls=handle,
            mode=mode,
            resume=resume,
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

        if outcome.status == "succeeded":
            final_status = RunStatus.SUCCEEDED
            logger.info("run %s SUCCEEDED (outputs=%d nodes)", run_id, len(outcome.outputs))
        elif outcome.status == "cancelled":
            final_status = RunStatus.CANCELLED
            logger.info("run %s CANCELLED", run_id)
            self.recorder.record(
                run_id=run_id,
                tenant_id=tenant_id,
                node_id=None,
                kind=RunEventKind.RUN_CANCELLED,
                payload={"reason": "operator"},
            )
        else:
            final_status = RunStatus.FAILED
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

    def _load_mode(self, run_id: uuid.UUID) -> str:
        """Read the run's execution mode ('live' | 'dry_run') from the DB.

        Set once at run creation and immutable, so a single read at drive start
        is authoritative for the whole run. Defaults to LIVE if the row or column
        is somehow absent — fail safe toward really executing, since a dry-run
        that silently ran for real would be the dangerous direction only if the
        opposite; here defaulting to LIVE matches the column default."""
        with self.session_factory.session() as s:
            run = s.get(Run, run_id)
            if run is None:
                return RunMode.LIVE
            return run.mode or RunMode.LIVE

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
