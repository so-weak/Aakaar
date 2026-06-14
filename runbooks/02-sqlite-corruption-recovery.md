# 02 — SQLite corruption recovery

## Symptoms

- API logs: `sqlite3.DatabaseError: database disk image is malformed`, or
  `file is not a database`, often on a subset of queries only.
- `sqlite3 data/aakaar.sqlite "PRAGMA integrity_check;"` prints anything
  other than the single word `ok`.
- The process crash-loops at startup during `alembic upgrade head`.

Corruption on SQLite is rare in this deployment shape (single process,
WAL, `busy_timeout=5000`) and almost always traces back to one of:
out-of-space during a write, the file being copied/edited while live,
two API processes pointed at the same file over a network filesystem
(NFS/SMB file locking does not work — never put `data/` on a network mount),
or failing storage underneath.

## Step 0 — stop the bleeding

```bash
../dev-stop.sh                       # or: docker compose stop aakaar-api
df -h .                              # rule out a full disk FIRST
cd ~/Codes/Aakaar/aakaar
cp -p data/aakaar.sqlite      /tmp/corrupt-$(date +%s).sqlite      # work on copies
cp -p data/aakaar.sqlite-wal  /tmp/ 2>/dev/null || true
cp -p data/aakaar.sqlite-shm  /tmp/ 2>/dev/null || true
```

Do not run *any* write against the original file until you've copied it.

## Step 1 — decide: restore or repair?

If you have a recent verified backup
([01-sqlite-backup-restore](01-sqlite-backup-restore.md)), **restore it** —
that path is deterministic and takes minutes. Repair (below) is for when the
backup is too old or absent.

How much would a restore lose?

```bash
sqlite3 backups/<latest>/aakaar.sqlite \
  "SELECT max(started_at) FROM runs; SELECT max(at) FROM audit_log;"
```

The object store and vault are plain directories and are usually intact even
when the DB is not — a DB-only restore from backup plus the live `objects/`
and `vault/` dirs is often the best combination.

## Step 2 — repair attempt (on a copy)

```bash
cd /tmp

# 2a. Let SQLite replay/checkpoint the WAL into the main file.
sqlite3 corrupt-*.sqlite "PRAGMA wal_checkpoint(TRUNCATE);" || true
sqlite3 corrupt-*.sqlite "PRAGMA integrity_check;"

# 2b. If still broken: export everything readable and rebuild.
sqlite3 corrupt-*.sqlite ".recover" > recovered.sql      # sqlite3 >= 3.29
sqlite3 recovered.sqlite < recovered.sql
sqlite3 recovered.sqlite "PRAGMA integrity_check;"        # must be: ok
```

`.recover` skips unreadable pages — compare row counts against expectations
before adopting the result:

```bash
for t in tenants users workflows workflow_versions runs capability_grants audit_log; do
  printf '%-20s' "$t"; sqlite3 recovered.sqlite "SELECT count(*) FROM $t;"
done
```

## Step 3 — put the repaired DB into service

```bash
cd ~/Codes/Aakaar/aakaar
mv data/aakaar.sqlite     data/aakaar.sqlite.corrupt
rm -f data/aakaar.sqlite-wal data/aakaar.sqlite-shm     # stale WAL must not be replayed onto the new file
cp /tmp/recovered.sqlite  data/aakaar.sqlite
.venv/bin/alembic upgrade head
../dev.sh                                               # or docker compose start
```

On startup the orchestrator marks any QUEUED/RUNNING/PAUSED runs as FAILED
(`recovered N interrupted run(s)` in the log) — expected after any unclean
stop.

## Step 4 — verify and follow up

- `curl -s localhost:8000/healthz` → `{"status":"ok"}`.
- Log in, list workflows, start a trivial run, fetch one artifact.
- Spot-check that grants still resolve (vault files were untouched, but a
  recovered `capability_grants` table must still point at existing
  `data/vault/<tenant>/grants/...` files; a missing file surfaces as a
  `VaultNotFound` error on first use of that grant).
- Find the root cause before closing: disk health (`smartctl`), free space
  monitoring, exactly one API process per DB file, no network filesystems.
- If rows were lost, reconcile what you can from
  `data/audit/audit.jsonl` — it is an append-only file mirror of `audit_log`
  and frequently survives DB damage.

Escalate with [escalation.md](escalation.md) if `.recover` output is missing
tables outright or tenants report missing workflows after restore.
