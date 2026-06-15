"""Durable run state — layer checkpoints, resume seeds, and the event outbox.

This is the in-process answer to "survive an API restart, resume mid-DAG"
(the thing a Temporal-backed executor would give for free). Three pieces:

  - `CheckpointStore` — after the executor settles a DAG layer, persist one
    `run_checkpoints` row (completed node ids + the redacted output-env
    snapshot) and mirror the newest onto `runs.checkpoint` for a single-read
    fast path. `(run_id, layer_index)` is unique, so a re-driven layer
    overwrites rather than duplicates.

  - `ResumeState` — the snapshot recovery hands the executor on restart: the
    env to seed, the set of already-completed node ids to skip, and the layer
    to resume from. Built from `runs.checkpoint` (or, defensively, the newest
    `run_checkpoints` row).

  - `EventOutbox` — drives the `run_events.published`/`published_at` flags so
    the in-process WS fan-out is at-least-once across a restart. The recorder
    writes a row unpublished; the outbox marks it published only AFTER the
    subscriber dispatch returns, and a startup sweep replays anything still
    unpublished. No broker — this is the same single-process pub/sub the
    `EventBroker` already does, made restart-safe.

Everything here writes through the existing `SessionFactory` with its own
short transaction, the same convention as `DbEventRecorder` — no session is
held across an await.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from aakaar.db.models import Run, RunCheckpoint, RunEvent
from aakaar.db.session import SessionFactory

logger = logging.getLogger(__name__)


# Outputs may carry user-typed secrets (OTPs, free-form responses) or
# credential-shaped fields. The env snapshot is persisted, so redact it with
# the same key set the orchestrator uses for runs.outputs before it lands.
_REDACT_KEYS = {"password", "token", "api_key", "secret", "authorization"}


def redact_env(env: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Redact credential-shaped keys from an output-env snapshot before it is
    persisted to a checkpoint. Mirrors `orchestrator._redact_outputs` so a
    resumed run and a finished run scrub identically."""
    out: dict[str, dict[str, Any]] = {}
    for nid, payload in env.items():
        if not isinstance(payload, dict):
            out[nid] = {"value": "<non-dict output>"}
            continue
        out[nid] = {
            k: ("<redacted>" if k.lower() in _REDACT_KEYS else v)
            for k, v in payload.items()
        }
    return out


@dataclass(frozen=True, slots=True)
class ResumeState:
    """What recovery hands the executor to resume a run mid-DAG.

    `next_layer_index` is the first layer that has NOT completed — the executor
    skips every layer before it and seeds its env from `env`. `completed_ids`
    are the node ids whose outputs are already in `env`; the executor must not
    re-dispatch or re-emit events for them (the financial-integrity rule).
    """

    next_layer_index: int
    env: dict[str, dict[str, Any]]
    completed_ids: frozenset[str]


@dataclass
class CheckpointStore:
    """Persists per-layer checkpoints and mirrors the newest onto runs.checkpoint."""

    session_factory: SessionFactory

    def save_layer(
        self,
        *,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        layer_index: int,
        completed_node_ids: list[str],
        env: dict[str, dict[str, Any]],
    ) -> None:
        """Upsert the checkpoint for `layer_index` and mirror it onto the run.

        Idempotent on `(run_id, layer_index)`: a re-driven layer (e.g. after a
        resume that re-settles a boundary) overwrites the existing row rather
        than violating the unique constraint. The env is redacted here so a
        secret never reaches the checkpoint table.
        """
        redacted = redact_env(env)
        now = datetime.now(UTC)
        with self.session_factory.session() as s:
            row = s.scalar(
                select(RunCheckpoint).where(
                    RunCheckpoint.run_id == run_id,
                    RunCheckpoint.layer_index == layer_index,
                )
            )
            if row is None:
                row = RunCheckpoint(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    layer_index=layer_index,
                    completed_node_ids=list(completed_node_ids),
                    env=redacted,
                    created_at=now,
                )
                s.add(row)
            else:
                row.completed_node_ids = list(completed_node_ids)
                row.env = redacted
            # Mirror onto runs.checkpoint for the single-read recovery fast path.
            run = s.get(Run, run_id)
            if run is not None:
                run.checkpoint = {
                    "layer_index": layer_index,
                    "completed_node_ids": list(completed_node_ids),
                    "env": redacted,
                }
            s.commit()
        logger.debug(
            "checkpoint saved run_id=%s layer=%d nodes=%d",
            run_id,
            layer_index,
            len(completed_node_ids),
        )

    def load_resume_state(self, run_id: uuid.UUID) -> ResumeState | None:
        """Build the resume snapshot for a run, or None if it has no checkpoint.

        Prefers `runs.checkpoint` (the single-read mirror). Falls back to the
        highest `run_checkpoints.layer_index` if the mirror is somehow absent
        but per-layer rows exist (belt and suspenders for a crash between the
        per-layer insert and the mirror update — though both happen in one
        commit, so this is purely defensive).
        """
        with self.session_factory.session() as s:
            run = s.get(Run, run_id)
            mirror = run.checkpoint if run is not None else None
            if isinstance(mirror, dict) and "layer_index" in mirror:
                return _resume_from_dict(mirror)
            row = s.scalar(
                select(RunCheckpoint)
                .where(RunCheckpoint.run_id == run_id)
                .order_by(RunCheckpoint.layer_index.desc())
                .limit(1)
            )
            if row is None:
                return None
            return _resume_from_dict(
                {
                    "layer_index": row.layer_index,
                    "completed_node_ids": list(row.completed_node_ids or []),
                    "env": dict(row.env or {}),
                }
            )


def _resume_from_dict(cp: dict[str, Any]) -> ResumeState:
    layer_index = int(cp.get("layer_index", -1))
    completed = cp.get("completed_node_ids") or []
    env = cp.get("env") or {}
    return ResumeState(
        next_layer_index=layer_index + 1,
        env={k: dict(v) for k, v in env.items() if isinstance(v, dict)},
        completed_ids=frozenset(str(n) for n in completed),
    )


@dataclass
class EventOutbox:
    """At-least-once, restart-safe fan-out of run events via the in-process broker.

    The recorder persists each `run_events` row with `published=False`. This
    outbox dispatches the event to live subscribers and only then flips the row
    to `published=True` — so a crash between persist and dispatch leaves the
    row unpublished and the startup `sweep()` replays it. Dispatch is therefore
    at-least-once: a subscriber that reconnects after a restart may see an event
    again, which the UI dedupes on `(run_id, sequence)`.

    `publish_fn` is the broker's `publish(run_id, message)` — kept as a plain
    callable so this module never imports the services layer (it sits below it).
    """

    session_factory: SessionFactory
    publish_fn: Any = None
    """Callable(run_id: UUID, message: dict) -> None. None disables fan-out
    (the row is still marked published so the outbox doesn't grow unbounded)."""

    def dispatch(
        self,
        *,
        run_id: uuid.UUID,
        sequence: int,
        node_id: str | None,
        kind: str,
        payload: dict[str, Any],
        at: datetime,
    ) -> None:
        """Fan out one freshly-recorded event, then mark its row published.

        Called inline after `recorder.record`. A publish failure leaves the row
        unpublished so the next `sweep()` retries it — the event is never lost,
        only possibly delayed.
        """
        delivered = self._deliver(
            run_id=run_id,
            message={
                "sequence": sequence,
                "node_id": node_id,
                "kind": kind,
                "payload": payload,
                "at": at.isoformat(),
            },
        )
        if delivered:
            self._mark_published(run_id=run_id, sequence=sequence)

    def sweep(self) -> int:
        """Replay every still-unpublished event in run+sequence order.

        Called once on startup (and safe to call periodically). Returns the
        number of events (re)dispatched. Ordering is `(run_id, sequence)` via
        the `ix_run_events_outbox` index so a subscriber sees a run's timeline
        in order.
        """
        published = 0
        # Snapshot the pending rows, then dispatch outside the read so a slow
        # subscriber can't hold the read transaction open.
        with self.session_factory.session() as s:
            rows = list(
                s.scalars(
                    select(RunEvent)
                    .where(RunEvent.published.is_(False))
                    .order_by(RunEvent.run_id, RunEvent.sequence)
                )
            )
            pending = [
                (
                    r.run_id,
                    r.sequence,
                    r.node_id,
                    r.kind,
                    dict(r.payload or {}),
                    r.at,
                )
                for r in rows
            ]
        for run_id, sequence, node_id, kind, payload, at in pending:
            delivered = self._deliver(
                run_id=run_id,
                message={
                    "sequence": sequence,
                    "node_id": node_id,
                    "kind": kind,
                    "payload": payload,
                    "at": at.isoformat() if at is not None else None,
                },
            )
            if delivered:
                self._mark_published(run_id=run_id, sequence=sequence)
                published += 1
        if published:
            logger.info("event outbox swept %d unpublished event(s)", published)
        return published

    # --- internals ---------------------------------------------------------

    def _deliver(self, *, run_id: uuid.UUID, message: dict[str, Any]) -> bool:
        if self.publish_fn is None:
            # No subscriber sink wired (unit tests / headless): treat as
            # delivered so the row is marked and the outbox stays bounded.
            return True
        try:
            self.publish_fn(run_id, message)
            return True
        except Exception:
            logger.warning(
                "event outbox: publish failed run_id=%s seq=%s; leaving unpublished",
                run_id,
                message.get("sequence"),
                exc_info=True,
            )
            return False

    def _mark_published(self, *, run_id: uuid.UUID, sequence: int) -> None:
        now = datetime.now(UTC)
        with self.session_factory.session() as s:
            row = s.scalar(
                select(RunEvent).where(
                    RunEvent.run_id == run_id, RunEvent.sequence == sequence
                )
            )
            if row is None:
                return
            if row.published:
                return
            row.published = True
            row.published_at = now
            s.commit()


# Run event kinds emitted by the durability/resume path. Kept here (not in the
# DB RunEventKind enum, which is frozen) as plain strings — the `kind` column is
# free-form String(32) and the recorder accepts any str.
EVENT_RUN_RESUMED_FROM_CHECKPOINT = "run_resumed_ckpt"


@dataclass
class _NullOutbox:
    """An outbox that does nothing — used when durability is not wired so the
    executor can call a uniform interface. Never persists or publishes."""

    calls: int = field(default=0)

    def dispatch(self, **_kwargs: Any) -> None:
        self.calls += 1
