# 05 — Reconciliation breaks (internal ledger vs bank statement)

Fetches two statements — the internal ledger over SFTP and the bank's
reconciliation export through an authenticated web session — isolates the
unmatched (break) rows with a deterministic pandas transform, and pushes the
result to the operations webhook with both source files referenced as evidence.

```
stamp (time.now) ─────────────────────────────────────────────┐
ledger_login (cap.sftp_login) → fetch_internal (cap.sftp_read) ─┤
bank_login (cap.web_login) → fetch_bank (cap.file_download) ─→ breaks (cap.data_transform) ─┤
                                                                                            └─→ notify (cap.webhook_send)
```

## How it works

- **stamp** stamps the run date (`time.now`); `${stamp.ist_date}` lands in the
  webhook payload so a saved DAG never carries a literal date.
- **ledger_login / fetch_internal** open an SFTP session and stream the internal
  ledger CSV into managed storage. The 3-attempt retry absorbs transient SFTP
  drops. Only `account_alias` is in the DAG — host, port, and the username /
  key live on the grant.
- **bank_login / fetch_bank** log into the bank portal (`cap.web_login`
  auto-discovers the form and handles captcha via the run's HITL channel) and
  download the reconciliation export by its visible name (`target_hint`). No
  selector is hard-coded.
- **breaks** runs the bank export through `cap.data_transform`: a `filter`
  keeps rows whose `match_status != MATCHED`, then a `sort` orders them by
  `amount` descending. The result is written back as a new CSV; the node also
  returns the row count and column list. This is the break detection — fully
  deterministic, server-local, no LLM.
- **notify** POSTs the break count, the breaks file URI, and both source-file
  URIs to the ops endpoint through the SSRF guard. Each `${...}` is a whole
  value (a ref must occupy the entire string — embedding is unsupported), so
  the payload is assembled as object keys, not interpolated text.

The bank export is assumed to carry a `match_status` column (banks return a
reconciliation/exception file with a per-row status). To key on a different
column or value, edit the `filter` op in `breaks`.

## Required grants (tenant admin, once)

```json
{"capability_ref": "cap.sftp_login", "account_alias": "primary",
 "secrets": {"username": "...", "password": "...", "private_key": "", "private_key_passphrase": ""},
 "input_defaults": {"host": "sftp.example.com", "port": 22}}

{"capability_ref": "cap.sftp_read", "account_alias": "default", "secrets": {}, "input_defaults": {}}

{"capability_ref": "cap.web_login", "account_alias": "primary",
 "secrets": {"username": "...", "password": "..."},
 "input_defaults": {"login_url": "https://portal.bank.example/login"}}

{"capability_ref": "cap.file_download", "account_alias": "default", "secrets": {}, "input_defaults": {}}

{"capability_ref": "cap.data_transform", "account_alias": "default", "secrets": {}, "input_defaults": {}}

{"capability_ref": "cap.webhook_send", "account_alias": "default", "secrets": {}, "input_defaults": {}}
```

`cap.sftp_login` declares all four secret names; supply empty strings for the
unused auth method (the grant API requires the exact declared set).

## Before importing

- Edit `remote_path` (internal ledger), the `target_hint` (bank export name),
  and the `notify` URL.
- If the bank export's break column isn't `match_status` / value isn't
  `MATCHED`, edit the `filter` op in `breaks`.

## Stage-2 features exercised

- **Dry-run** — start this run with `"mode": "dry_run"` (see
  [../README.md](../README.md)). `cap.sftp_read`, `cap.file_download`,
  `cap.data_transform`, and `cap.webhook_send` declare no `side_effecting`
  flag, so a dry-run **simulates** them (logging `would_run` instead of moving
  files or POSTing) and walks the full topology. Use it to validate wiring
  before pointing at the real bank.
- **Tamper-evident audit** — every node is written to the hash-chained ledger;
  prove the recon ran untampered with `GET /audit/verify` and stream the chain
  with `GET /audit/export`.

Import + run: see [../README.md](../README.md).
