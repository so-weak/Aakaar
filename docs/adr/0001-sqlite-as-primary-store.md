# ADR 0001: SQLite as the primary store

- **Status:** Accepted
- **Date:** 2026-06-15
- **Deciders:** Platform engineering

## Context

Aakaar must run inside a regulated bank with a hard constraint: **no
third-party infrastructure**. A managed Postgres, a Redis, a Temporal cluster,
or a cloud database are all off the table — operations is a small team running a
handful of on-prem hosts, often air-gapped. We still need durable, transactional
storage for tenants, users, workflows, runs, audit, retention policies, and the
event log.

The data shapes are modest: a single bank tenant generates thousands (not
billions) of runs; the working set fits comfortably on one host's disk. The
write pattern is single-writer in practice — one API process owns the database.
What we cannot compromise on is **transactional integrity** (a run, its events,
and its audit row must commit atomically) and **operational simplicity** (a
DBA-free backup/restore an ops team can run from a shell).

## Decision

Use **SQLite** as the primary store, accessed through SQLAlchemy. The default
`AAKAAR_DB_URL` is `sqlite:///<data_dir>/aakaar.sqlite`. Schema is owned by
Alembic migrations (`aakaar/aakaar/db/migrations/`). Postgres remains a
*supported* backend for sites that already operate one
(`AAKAAR_DB_URL=postgresql+psycopg://...`), but it is never *required*, and no
feature assumes Postgres-only capabilities.

Concretely:

- One database file per deployment; WAL-aware backup via `sqlite3 .backup`
  (see [runbooks/01-sqlite-backup-restore.md](../../runbooks/01-sqlite-backup-restore.md)).
- The application is single-process / single-writer; concurrency that the DB
  cannot serialize (e.g. the audit hash chain's read-max-seq-then-insert) is
  serialized in-process with a lock, with a unique index as the durable
  backstop (`uq_audit_tenant_seq`).
- Cross-process coordination primitives a bigger stack would push to Redis or a
  broker (run event fan-out, checkpoint/resume, human-task escalation) are
  implemented in-process against the same SQLite store (see ADR 0002).

## Consequences

**Positive**

- Zero infrastructure to provision, patch, or secure beyond the OS and a single
  file. Backup is a file copy; restore is a file move. The entire data estate
  (`aakaar.sqlite`, `objects/`, `vault/`, `vector/`, `audit/`) lives under one
  `data_dir`.
- Transactions are real and local: a run row, its events, and its audit entry
  commit in one place with no distributed-transaction problem.
- Tests run against the identical engine with no service to stand up, so CI and
  production behave the same.

**Negative / accepted trade-offs**

- **No horizontal write scaling and no built-in HA.** SQLite is a single-host
  store. Availability is a host/restore concern, not a database-cluster
  concern; this is acceptable for the target's volume and is the price of the
  no-infra constraint. Sites needing HA point `AAKAAR_DB_URL` at their own
  Postgres.
- **No database-level row security on SQLite** — tenant isolation is
  application-level (see ADR 0003). Anyone with filesystem access to the file
  can read every tenant's metadata, so the file's OS permissions and host
  hardening are part of the trust boundary.
- **Single-writer assumption is load-bearing.** Running two API processes
  against one SQLite file is unsupported; the in-process locks and the
  at-least-once event outbox assume one writer.

## Alternatives considered

- **Require Postgres.** Rejected: violates the no-infra constraint and burdens
  every site with a DBA.
- **Temporal / a workflow engine for durability.** Rejected: heavy external
  infra; we get the durability we need from in-process checkpoints (ADR 0002).
- **Redis for event fan-out / coordination.** Rejected: another service to run;
  the single-process broker + DB outbox covers it.
