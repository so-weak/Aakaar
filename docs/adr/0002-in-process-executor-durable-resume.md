# ADR 0002: In-process DAG executor with durable checkpoint/resume

- **Status:** Accepted
- **Date:** 2026-06-15
- **Deciders:** Platform engineering

## Context

A workflow is a DAG of capability nodes executed layer by layer. A bank runs
these against money-moving systems, so two properties are non-negotiable:

1. **Survive an API restart mid-run** without re-executing already-completed
   side-effecting nodes (you must never send a payment twice because the process
   bounced).
2. **No external orchestrator.** A Temporal/Cadence-style engine would give
   durable execution for free but is exactly the third-party infrastructure the
   deployment forbids (ADR 0001).

We therefore need durable execution semantics from an in-process executor backed
only by SQLite.

## Decision

Run the DAG in-process and make it restart-safe with three durable primitives
(`aakaar/aakaar/interpreter/durability.py`):

- **`CheckpointStore`** — after the executor settles a DAG layer, it persists one
  `run_checkpoints` row (the completed node ids + a **redacted** output-env
  snapshot) and mirrors the newest onto `runs.checkpoint` for a single-read fast
  path. `(run_id, layer_index)` is unique, so a re-driven layer overwrites
  rather than duplicates.
- **`ResumeState`** — on startup, `orchestrator.recover_interrupted_runs()`
  reconstructs the env to seed, the set of already-completed node ids to skip,
  and the layer to resume from. The executor skips every completed node and
  never re-dispatches or re-emits its events.
- **`EventOutbox`** — run events are persisted `published=False`; the in-process
  broker fan-out marks a row published only *after* dispatch returns, and a
  startup `sweep()` replays anything still unpublished. Fan-out is therefore
  **at-least-once** across a restart; the UI dedupes on `(run_id, sequence)`.

A recovered run is bounded by `AAKAAR_MAX_RUN_RESUMES` (default 5): a poison run
that always crashes the same layer is failed rather than resumed forever.

Checkpoint env snapshots are redacted with the same credential-key set the
orchestrator uses for `runs.outputs` (`redact_env`), so a secret never reaches
the checkpoint table.

## Consequences

**Positive**

- Durable resume with **no external workflow engine** — entirely SQLite +
  in-process logic, satisfying the no-infra constraint.
- The financial-integrity rule (never re-run a completed side-effecting node) is
  enforced by skipping `completed_ids` on resume.
- Event delivery is restart-safe (at-least-once) without a message broker.

**Negative / accepted trade-offs**

- **Checkpoint granularity is per-layer, not per-node.** If the process dies
  mid-layer, every node in that layer that had not been recorded complete is
  re-attempted on resume. Authors must keep side-effecting capabilities
  idempotent where the business allows, and the **dry-run** simulation path
  (ADR-referenced in the capability guide) exists to rehearse a DAG before a
  live run.
- **Single-process only.** The outbox and the in-process broker assume one
  writer (ADR 0001); there is no cross-node work distribution.
- **Recovery is best-effort on a corrupt checkpoint.** A malformed
  `runs.checkpoint` falls back to the highest `run_checkpoints` row; if neither
  is usable the run cannot resume and is left for operator inspection (see
  [runbooks/07-run-stuck-or-paused.md](../../runbooks/07-run-stuck-or-paused.md)).

## Alternatives considered

- **Temporal / Cadence.** Rejected: external infra.
- **Re-run the whole DAG on restart.** Rejected: would re-fire completed
  side-effecting nodes — unacceptable for payments.
- **No durability (fail interrupted runs outright).** Rejected: a routine deploy
  would orphan in-flight work; the checkpoint cost is small and the UX/integrity
  win is large.
