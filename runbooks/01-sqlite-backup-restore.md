# 01 — SQLite backup & restore

The primary store is a single SQLite file, by default
`aakaar/data/aakaar.sqlite` (override: `AAKAAR_DB_URL` /
`AAKAAR_DATA_DIR`). The API opens it in **WAL mode** with a 5s busy
timeout (`PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=5000` — set on every
connect in `aakaar/aakaar/db/session.py`), which means:

- Live state spans **three files**: `aakaar.sqlite`, `aakaar.sqlite-wal`,
  `aakaar.sqlite-shm`. Copying only the `.sqlite` file while the API is
  running loses every transaction still in the WAL.
- **Never `cp` a live database.** Use `sqlite3 .backup`, which takes a
  consistent snapshot through the SQLite API even while the server is writing.

A complete backup also needs the sibling data dirs — the DB stores metadata,
but artifacts, secrets, and the planner index live on disk next to it:

| Path | Contents | Loss impact |
|------|----------|-------------|
| `data/aakaar.sqlite` | tenants, users, workflows, runs, grants, audit_log | everything |
| `data/objects/` | object store (run artifacts, attachments) | artifact downloads 404 |
| `data/vault/` | per-tenant secret files (Fernet-encrypted if keyed) | every credentialed capability fails |
| `data/vector/` | Chroma index for planner capability search | rebuildable; planner quality degrades until reindexed |
| `data/audit/audit.jsonl` | append-only mirror of `audit_log` | forensics only; DB copy remains |

## Take a backup (online, safe)

```bash
cd ~/Codes/Aakaar/aakaar    # adjust to your install
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p backups/$STAMP

# 1. Consistent DB snapshot (works while the API is running, thanks to WAL).
sqlite3 data/aakaar.sqlite ".backup 'backups/$STAMP/aakaar.sqlite'"

# 2. Verify the snapshot BEFORE trusting it.
sqlite3 "backups/$STAMP/aakaar.sqlite" "PRAGMA integrity_check;"   # must print: ok

# 3. Files alongside the DB. rsync is restartable and preserves modes
#    (vault files are 0600 — keep them that way).
rsync -a data/objects/ "backups/$STAMP/objects/"
rsync -a data/vault/   "backups/$STAMP/vault/"
rsync -a data/audit/   "backups/$STAMP/audit/"
# data/vector is optional — it can be rebuilt, but copying avoids a reindex:
rsync -a data/vector/  "backups/$STAMP/vector/"

tar czf "backups/aakaar-$STAMP.tar.gz" -C backups "$STAMP" && rm -rf "backups/$STAMP"
```

Schedule it with cron; keep at least 7 dailies off-box. The tarball contains
**secret material** (the vault dir) — store it with the same care as the
vault itself, and prefer running with `AAKAAR_VAULT_KEY` set so the vault
files inside the backup are ciphertext (see
[03-vault-key-rotation](03-vault-key-rotation.md)).

> Docker deployments: the same paths live in the `aakaar-data` volume at
> `/data`. Run the commands inside the container
> (`docker compose exec aakaar-api sqlite3 /data/aakaar.sqlite ...`) or mount
> the volume into a utility container.

## Restore

```bash
cd ~/Codes/Aakaar/aakaar

# 1. STOP the API first — restoring under a live process corrupts the WAL.
../dev-stop.sh            # dev; in docker: docker compose stop aakaar-api

# 2. Move the damaged state aside (never delete until the restore is verified).
mv data data.broken.$(date +%s)
mkdir data

# 3. Unpack the chosen backup.
tar xzf backups/aakaar-<STAMP>.tar.gz
mv <STAMP>/aakaar.sqlite data/
mv <STAMP>/objects data/ ; mv <STAMP>/vault data/ ; mv <STAMP>/audit data/
[ -d <STAMP>/vector ] && mv <STAMP>/vector data/

# 4. Sanity-check before boot.
sqlite3 data/aakaar.sqlite "PRAGMA integrity_check;"            # ok
sqlite3 data/aakaar.sqlite "SELECT count(*) FROM tenants;"      # plausible number

# 5. Apply any migrations newer than the backup, then start.
.venv/bin/alembic upgrade head
../dev.sh                  # or: docker compose start aakaar-api
```

After boot, expect a startup log line like
`recovered N interrupted run(s) -> FAILED on startup` — runs that were
QUEUED/RUNNING/PAUSED at backup time are deliberately marked FAILED with
`"Run interrupted by a server restart"` (the in-process executor cannot
re-attach). Re-launch them with `POST /runs/{run_id}/rerun`.

## Quarterly restore drill

A backup that has never been restored is a hope, not a backup.

1. Restore the latest tarball into a scratch dir and boot a throwaway API
   against it:
   ```bash
   mkdir -p /tmp/aakaar-drill && tar xzf backups/aakaar-<STAMP>.tar.gz -C /tmp/aakaar-drill
   cd ~/Codes/Aakaar/aakaar
   AAKAAR_DATA_DIR=/tmp/aakaar-drill/<STAMP> \
     AAKAAR_DB_URL="sqlite:////tmp/aakaar-drill/<STAMP>/aakaar.sqlite" \
     AAKAAR_JWT_SECRET=drill-only AAKAAR_SCHEDULER_ENABLED=false \
     AAKAAR_BROWSER_POOL=none AAKAAR_REMOTE_EXEC_ENABLED=false \
     .venv/bin/uvicorn aakaar.api.main:app --port 8999
   ```
   (`AAKAAR_SCHEDULER_ENABLED=false` so the drill instance doesn't fire real
   schedules; remote exec off so no agent traffic.)
2. Log in as a known tenant user, open a workflow, fetch one artifact via
   `GET /objects?uri=...`.
3. Confirm a vault-backed grant still decrypts: run a workflow that uses it,
   or check the logs for vault errors at startup.
4. Record the drill date and time-to-restore in your ops log; tear down
   `/tmp/aakaar-drill`.

## Related

- Corrupted DB that won't pass `integrity_check`:
  [02-sqlite-corruption-recovery](02-sqlite-corruption-recovery.md)
- Vault key handling in backups: [03-vault-key-rotation](03-vault-key-rotation.md)
