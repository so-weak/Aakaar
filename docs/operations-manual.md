# Aakaar Operations Manual

**Audience:** the bank's operations team running a deployed Aakaar instance.
**Scope:** day-2 operations — backup/restore, upgrade/rollback, the background
sweep tasks, and health/observability. Incident-specific procedures live in
[runbooks/](../runbooks/); this manual is the steady-state companion and points
into them.

Commands assume the standard layout: backend venv at `aakaar/.venv`, data under
`aakaar/data/` (override with `AAKAAR_DATA_DIR`). Adjust paths for your install
or container mounts.

---

## 1. What's on disk

Everything stateful lives under one `data_dir` (ADR
[0001](adr/0001-sqlite-as-primary-store.md)):

| Path | Contents | Loss impact |
|------|----------|-------------|
| `data/aakaar.sqlite` | tenants, users, workflows, runs, grants, approvals, retention, `audit_log` | total — the system of record |
| `data/objects/` | object store (run artifacts, attachments) | artifact downloads 404 |
| `data/vault/` | per-tenant secret files (Fernet-encrypted if keyed) | every credentialed capability fails |
| `data/vector/` | Chroma index for planner capability search | rebuildable; planner quality degrades until reindexed |
| `data/audit/audit.jsonl` | append-only mirror of `audit_log` | forensics convenience only; the DB copy is canonical |

The database is opened in **WAL mode** with a 5s busy timeout. Two consequences
for ops: (1) you must **never `cp` a live database** — copy via `sqlite3
.backup`; (2) the deployment is **single-process / single-writer** — do not run
two API processes against one SQLite file.

---

## 2. Backup and restore

The authoritative, step-by-step procedure (WAL-aware `sqlite3 .backup`,
`integrity_check`, the object/vault/audit sync, and the stop-before-restore
rule) is **[runbooks/01-sqlite-backup-restore.md](../runbooks/01-sqlite-backup-restore.md)**.
The essentials:

**Backup (safe while the API runs):**

```bash
STAMP=$(date +%Y%m%dT%H%M%S)
sqlite3 data/aakaar.sqlite ".backup 'backups/$STAMP/aakaar.sqlite'"
sqlite3 "backups/$STAMP/aakaar.sqlite" "PRAGMA integrity_check;"   # must print: ok
rsync -a data/objects/ "backups/$STAMP/objects/"
rsync -a data/vault/   "backups/$STAMP/vault/"
rsync -a data/audit/   "backups/$STAMP/audit/"
rsync -a data/vector/  "backups/$STAMP/vector/"   # optional; rebuildable
```

**Restore (STOP the API first — restoring under a live process corrupts the
WAL):** move the snapshot back into `data/`, then `PRAGMA integrity_check;` and a
sanity `SELECT count(*) FROM tenants;`. Full commands in the runbook.

**Backup hygiene for a bank:**

- The vault backup is only as good as its keys. If `AAKAAR_VAULT_KEY` is rotated,
  ensure the retired keys are retained until no backup that needs them is in your
  retention window — a vault snapshot is undecryptable without its key.
- Back up `audit/audit.jsonl` with the DB; an off-box copy of the audit mirror
  also serves as an external attestation of the chain head (ADR
  [0007](adr/0007-tamper-evident-audit.md)).
- Corruption symptoms (`database disk image is malformed`, `integrity_check`
  failures): [runbooks/02-sqlite-corruption-recovery.md](../runbooks/02-sqlite-corruption-recovery.md).

---

## 3. Upgrade and rollback

Schema is owned by **Alembic** (`aakaar/aakaar/db/migrations/`); the current head
is `0008_governance_durability`. Migrations are forward-only in normal operation;
each carries a `downgrade()` for emergency rollback.

**Upgrade:**

```bash
# 1. BACK UP FIRST (section 2) — a migration is a schema change to your system of record.
# 2. Stop the API.
# 3. Apply migrations:
cd aakaar && .venv/bin/alembic upgrade head
# 4. Start the API; watch the log for "lifespan: startup" and the startup sweeps.
```

`dev.sh` runs `alembic upgrade head` automatically on the inner loop; in
production, run it as an explicit, gated step against a backed-up database.

**Rollback:**

1. Prefer **restore-from-backup** for a clean revert (section 2) — it is the
   safest path and is required if a migration is not cleanly reversible on your
   data.
2. If you must step the schema back, `cd aakaar && .venv/bin/alembic downgrade
   <revision>` (e.g. `downgrade 0007_row_level_security`). Validate with `PRAGMA
   integrity_check;` and a read of a few core tables before restarting.
3. Roll the application binary/image back **in lockstep** with the schema — a
   newer app against an older schema (or vice versa) is unsupported.

**Compatibility note.** The governance/durability features (approvals, retention,
checkpoints, audit chaining) are introduced by `0008`. An app build that expects
them against a pre-`0008` schema will fail; keep app and schema versions matched.

---

## 4. Background sweep tasks (the lifespan)

On startup (`create_app` lifespan in `aakaar/aakaar/api/app.py`) the API runs a
fixed set of background tasks. Knowing what they are — and what they are **not** —
is essential for operating the system.

| Task | What it does | Cadence | Operational signal |
|------|--------------|---------|--------------------|
| **Superuser bootstrap** | Creates the first superuser from `AAKAAR_SUPERUSER_EMAIL`/`_PASSWORD` if absent | Once at startup | Log line at startup; rotate the bootstrap creds after first login |
| **Interrupted-run recovery** | `recover_interrupted_runs()` reconciles runs left mid-flight by a crashed/restarted process so the UI shows no perpetual zombies (ADR 0002) | Once at startup | `startup: interrupted-run recovery` log; bounded by `AAKAAR_MAX_RUN_RESUMES` |
| **Event-outbox sweep** | `event_outbox.sweep()` replays run events persisted but never fanned out (at-least-once); UI dedupes on `(run_id, sequence)` | Once at startup | `event outbox swept N unpublished event(s)` |
| **Workflow scheduler** | Cron + one-off workflow triggers; runs only if `AAKAAR_SCHEDULER_ENABLED=true` (default) | Every `AAKAAR_SCHEDULER_TICK_SECONDS` (default 5s) | Disable in environments that must not auto-fire |
| **Human-task escalation** | Escalates/expires governed `human.prompt` tasks past their SLA even when no run activity drives it | Every `AAKAAR_HUMAN_TASK_ESCALATION_TICK_SECONDS` (default 60s) | Inert when no `human.prompt` tasks are outstanding |
| **Recording expiry** | Expires abandoned activity-recording sessions (TTL ~2h) and tells their agents to stop capturing | Background loop | Inert when no recordings are in flight |

**What the lifespan does NOT do — important.** There is **no automatic retention
TTL sweep**. The retention service (`sweep_all_tenants()`) exists and is
tenant-safe, but it is **not** wired as a lifespan task (ADR
[0008](adr/0008-retention-legal-hold-erasure.md)). On-demand erasure, legal hold,
and policy management work fully via `/retention`, but **PII does not auto-expire
on its TTL** unless you drive the sweep yourself (an external scheduler invoking
an admin-authorized path, or a future lifespan task). Do not assume time-based
erasure happens without that wiring.

**Shutdown** is graceful: the broker link, human-task escalator, recording
service, scheduler, and browser pool are each torn down in `finally`, with
failures logged (never raised) so one slow teardown can't block the others.

---

## 5. Health, metrics, and logs

**Liveness:**

```bash
curl -s http://localhost:8000/healthz          # {"status":"ok"}
```

**Metrics** (Prometheus text; scraped locally, no external egress; on unless
`AAKAAR_METRICS_ENABLED=false`):

```bash
curl -s http://localhost:8000/metrics | grep aakaar_
```

Exposed series include:

- `aakaar_http_requests_total{method,path,status}` — request counter
- `aakaar_http_request_duration_seconds{method,path}` — latency histogram
- `aakaar_runs_total{status}` — workflow run outcomes

A 5xx spike or a climbing `aakaar_runs_total{status="failed"}` is the entry point
to [runbooks/06-high-error-rate.md](../runbooks/06-high-error-rate.md).

**Logs.** The API logs to stdout. Set `AAKAAR_LOG_FORMAT=json` for one-line JSON
records (`ts`, `level`, `logger`, `msg`, plus `request_id`/`run_id`/`tenant_id`
when in context) suitable for `jq` or a local collector. Every HTTP response
carries an `X-Request-ID` header that matches the `request_id` field in the logs
— quote it in incident tickets.

**Audit verification as a routine check.** Periodically confirm each tenant's
audit chain is intact and pin its head off-box:

```bash
# tenant admin token: verify your own tenant
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/audit/verify
# superuser: verify/export any tenant
curl -s -H "Authorization: Bearer $SU"  http://localhost:8000/audit/tenants/$TID/verify
curl -s -H "Authorization: Bearer $SU"  http://localhost:8000/audit/tenants/$TID/export > audit-$TID.jsonl
```

A `{"ok": false, ...}` result names the `first_broken_seq` and a `reason` — treat
it as a security incident (see the [whitepaper](security-whitepaper.md) §8 and
[SECURITY.md](../SECURITY.md)).

---

## 6. Routine operational tasks index

| Task | Where |
|------|-------|
| Take/restore a backup | [runbook 01](../runbooks/01-sqlite-backup-restore.md) |
| Recover a corrupt DB | [runbook 02](../runbooks/02-sqlite-corruption-recovery.md) |
| Rotate the vault key | [runbook 03](../runbooks/03-vault-key-rotation.md) |
| Agents offline / reconnect storm / key compromise | [runbook 04](../runbooks/04-agent-fleet-degradation.md) |
| Broker down | [runbook 05](../runbooks/05-broker-outage.md) |
| 5xx / latency / failing runs | [runbook 06](../runbooks/06-high-error-rate.md) |
| A run that won't finish | [runbook 07](../runbooks/07-run-stuck-or-paused.md) |
| Hand off an incident | [escalation template](../runbooks/escalation.md) |
| Configure retention / legal hold / erasure | `/retention` API; [compliance-mapping](compliance-mapping.md) |
| Verify/export the audit trail | `/audit/verify`, `/audit/export` (section 5) |
