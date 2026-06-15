# ADR 0007: Per-tenant hash-chained audit ledger

- **Status:** Accepted
- **Date:** 2026-06-15
- **Deciders:** Platform engineering, Risk/Controls

## Context

Regulators expect an audit trail that is not only complete but **tamper-evident**
— an investigator must be able to prove that no historical entry was edited,
deleted, or reordered. We must provide this on SQLite (ADR 0001), without an
external WORM store or ledger service, and an external auditor should be able to
re-verify the export **offline**.

## Decision

Write tenant-scoped audit rows as a **per-tenant hash chain**
(`aakaar/aakaar/services/audit/`):

- Each tenant-scoped row gets the next monotonic `seq` and an `entry_hash` =
  `sha256(prev_hash || canonical_payload(immutable fields))`
  (`chain.compute_entry_hash`). Linking each entry to its predecessor's hash
  makes any edit/delete/reorder break the recomputed chain at that point.
- The canonical serialization (`chain.canonical_payload`) is the single source of
  truth for both the writer (`recorder.py`) and the verifier (`ledger.py`):
  sorted-key JSON, fixed separators, microsecond-normalized timestamp (so the
  SQLite tz round-trip can't change the hashed bytes). It must never be
  reordered or re-encoded, or every historical hash would change.
- **Verification:** `GET /audit/verify` (tenant admin) recomputes the calling
  tenant's chain end-to-end and reports `ok`, `entries_checked`, and the
  `first_broken_seq` + `reason` on the first failure. Superusers can verify any
  tenant at `GET /audit/tenants/{tenant_id}/verify`.
- **Export:** `GET /audit/export` streams the chain as JSONL in `seq` order —
  including `seq`, `prev_hash`, and `entry_hash`, with the genesis row's NULL
  `prev_hash` emitted as the explicit sentinel the verifier substitutes — so an
  auditor can re-hash the exported bytes and reproduce `entry_hash` **offline**.
  Superuser cross-tenant export at `GET /audit/tenants/{tenant_id}/export`.
- **System rows** (`tenant_id is None`: superuser/bootstrap) are intentionally
  **not** chained — they get a NULL `seq` and form an append-only side log.
  Legacy rows written before chaining keep their NULL `seq` and form the
  pre-chain prefix; the chain begins at the first hashed row.
- The audit log is also mirrored append-only to `<data_dir>/audit/audit.jsonl`
  for on-host retention with no external log shipper. The DB table is canonical;
  the file is a convenience mirror.

Auditing never breaks the action it describes: each `record` commits in its own
short session, and a sink/audit failure is logged, never raised.

## Consequences

**Positive**

- Tamper-evidence with **no external ledger/WORM service** — sha256 + a unique
  index, all in SQLite.
- An external auditor can independently re-verify an export with nothing but the
  JSONL and the documented hashing rule.
- Concurrency is handled: a per-tenant in-process lock serializes the
  read-max-seq-then-insert, and `uq_audit_tenant_seq` is the durable backstop
  (retried on a torn-chain collision).

**Negative / accepted trade-offs**

- **Tamper-*evident*, not tamper-*proof*.** Someone with write access to SQLite
  can rewrite a row *and* recompute every subsequent hash to produce a
  self-consistent forged chain. Defenses: file permissions, the OS, and
  exporting/attesting the latest `entry_hash` to an external system on a
  schedule (a periodic export pins the chain head off-box).
- **System/legacy rows are unverifiable** by construction (NULL `seq`) — they are
  an append-only side log, not part of the proof.
- The canonical encoding is frozen forever; evolving it would invalidate every
  historical hash, so new fields must be added as `payload` content, not as new
  hashed top-level fields.

## Alternatives considered

- **External WORM / ledger database.** Rejected: third-party infra.
- **Sign each row with an asymmetric key.** Considered as a future hardening on
  top of the chain (signing the chain head would close the rewrite gap); not
  required for tamper-evidence and adds key-management surface, so deferred.
