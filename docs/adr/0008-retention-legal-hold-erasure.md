# ADR 0008: Tenant retention policies, legal hold, and right-to-erasure

- **Status:** Accepted
- **Date:** 2026-06-15
- **Deciders:** Platform engineering, Risk/Controls, Privacy

## Context

Two regulatory pressures pull in opposite directions. **Privacy** (e.g. data
minimisation, right-to-erasure) says PII must not be retained beyond its purpose
and must be erasable on request. **Litigation/investigation holds** say specific
records must be preserved untouched regardless of any retention schedule. The
**audit trail itself must outlive** what it describes — erasing a run must leave
proof that the run (and its erasure) happened.

## Decision

A tenant-scoped retention service
(`aakaar/aakaar/services/retention/service.py`) with three operations, exposed at
`/retention` (tenant admin):

- **Policies** — per `(tenant, resource_type)` with `ttl_days` (null = retain
  indefinitely). `GET/PUT /retention/policies[/{resource_type}]`. Two resource
  types are erasable today: `run` and `stored_object`.
- **Legal hold** — `POST /retention/legal-hold` sets/clears a `legal_hold` flag
  on a run or stored object. A held resource is **skipped by the sweep and
  refused by erasure** (TOCTOU-safe: the hold is re-checked under the write
  transaction).
- **Right-to-erasure** — `POST /retention/erase` scrubs one resource on demand.
  `run` erasure scrubs `inputs`/`outputs`/`error`/`checkpoint` and the redacted
  payloads mirrored onto its `run_events`, leaving a `_erased` marker and
  stamping `erased_at`; `stored_object` erasure deletes the underlying bytes,
  flips `status='erased'`, and stamps `erased_at`. The **row remains as an audit
  tombstone**; status/timeline metadata is retained. Erasure is idempotent and
  refuses with **409** while a hold is in force.

`sweep` / `sweep_all_tenants` apply the TTL policy (erase expired, non-held,
not-already-erased resources). Every erasure and hold change is written to the
tamper-evident audit ledger (ADR 0007); the audit log is **never itself a target**
of retention.

Cross-tenant or absent resources return an opaque **404**, consistent with the
rest of the API (ADR 0003).

## Consequences

**Positive**

- Privacy erasure and preservation holds coexist with a clear precedence: a
  **legal hold outranks an erasure request**.
- Erasure leaves a tombstone + audit entry, so "this was erased, by whom, when,
  why" is provable — the audit trail outlives the data.
- Tenant-scoped and admin-gated; the sweep cost scales with configured policies,
  not tenant count.

**Negative / accepted trade-offs**

- **The TTL sweep is not auto-wired into the API lifespan today.**
  `sweep_all_tenants()` exists and is tenant-safe, but it is **not** registered
  as a periodic startup task in `create_app` (the lifespan runs the run-recovery,
  event-outbox, scheduler, human-task escalator, and recording sweeps — not
  retention). On-demand erasure, legal hold, and policy management work fully via
  the API; automatic TTL expiry requires wiring the sweep on a schedule (an
  external cron calling an admin path, or a future lifespan task). This is called
  out so operators do not assume PII auto-expires without that wiring.
- Erasure is a **scrub-in-place tombstone**, not a hard delete of the row — by
  design, so the audit trail and timeline survive.
- Only `run` and `stored_object` are erasable; other resource types may carry a
  policy for reporting but are not scrubbed by this service.

## Alternatives considered

- **Hard-delete erased rows.** Rejected: destroys the audit tombstone and the
  ability to prove an erasure occurred.
- **Let erasure override a legal hold.** Rejected: a hold must win, or the
  control is meaningless.
