# Aakaar incident runbooks

Operational procedures for a deployed Aakaar instance. Each runbook is
self-contained: symptoms, diagnosis, fix, verification. Commands assume the
standard layout — repo at `~/Codes/Aakaar` (adjust paths), backend venv at
`aakaar/.venv`, data under `aakaar/data/` (or `$AAKAAR_DATA_DIR`).

| # | Runbook | When |
|---|---------|------|
| 01 | [SQLite backup & restore](01-sqlite-backup-restore.md) | Routine backups; restoring after data loss |
| 02 | [SQLite corruption recovery](02-sqlite-corruption-recovery.md) | `database disk image is malformed`, integrity_check failures |
| 03 | [Vault key rotation](03-vault-key-rotation.md) | Rotating `AAKAAR_VAULT_KEY`; first-time encryption of a plaintext vault |
| 04 | [Agent fleet degradation](04-agent-fleet-degradation.md) | Agents offline, placement failures, reconnect storms, key compromise |
| 05 | [Broker outage](05-broker-outage.md) | Rendezvous broker down or unreachable |
| 06 | [High error rate](06-high-error-rate.md) | 5xx spikes, failing runs, latency — driven from `/metrics` |
| 07 | [Run stuck or paused](07-run-stuck-or-paused.md) | A run that won't finish: prompt-blocked vs operator-paused vs zombie |
| — | [Escalation template](escalation.md) | Filling in an incident hand-off |

Quick orientation for any incident:

```bash
curl -s http://localhost:8000/healthz                    # liveness: {"status":"ok"}
curl -s http://localhost:8000/metrics | grep aakaar_     # request/run counters (no auth required)
ls -lh aakaar/data/                                      # aakaar.sqlite, objects/, vault/, vector/, audit/
tail -50 aakaar/data/audit/audit.jsonl                   # mirror of the audit_log table
```

The API logs to stdout; set `AAKAAR_LOG_FORMAT=json` for one-line JSON records
(`ts`, `level`, `logger`, `msg`, plus `request_id`/`run_id`/`tenant_id` when
in context) that you can grep or feed to `jq`. Every HTTP response carries an
`X-Request-ID` header that matches the `request_id` field in the logs.
