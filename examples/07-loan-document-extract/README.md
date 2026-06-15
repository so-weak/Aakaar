# 07 — Loan document extract → validate → archive

Fetches a loan application PDF over SFTP, extracts its text (and, when an LLM
is configured, structured fields) with `cap.doc_extract`, posts the result to
the loan-origination validation endpoint, and copies the source document to a
stable archival key.

```
stamp (time.now) ──────────────────────────────────────────────────────┐
login (cap.sftp_login) → fetch (cap.sftp_read) → extract (cap.doc_extract) → validate (cap.webhook_send) → archive (cap.file_manage)
```

## How it works

- **login / fetch** open an SFTP session and stream the application PDF into
  managed storage (`{uri, filename, size}`), with a 3-attempt retry. The DAG
  holds only `account_alias`; host and credentials live on the grant.
- **extract** reads the stored PDF with `cap.doc_extract` (`format: "pdf"`,
  pypdf under the backend's `doc` extra), returning the document text. The
  `extract` instruction ("applicant name, PAN, loan amount, property value")
  triggers an additional **read-only** LLM pass that returns structured JSON
  under `${extract.extracted}` **when a model is configured**; with no LLM,
  `extracted` is null and downstream still gets `${extract.text}`. The
  extraction is read-only — nothing is written by this node.
- **validate** delegates field validation to the origination system (LOS):
  it POSTs the extracted fields, the document text, and the document URI to the
  LOS validation endpoint through the SSRF guard. The endpoint owns the
  business rules (PAN format, amount/LTV limits, required fields); a non-2xx
  fails the node and stops the run before archival. Each `${...}` is a whole
  payload value (refs can't be embedded mid-string).
- **archive** copies the source PDF to an immutable object-store key
  (`loans/archive/application.pdf`) with `cap.file_manage` — the retained
  record of what was processed. `cap.file_manage` operates only on the managed
  object store, never the local filesystem.

`cap.ocr_extract` (Tesseract) is the sibling for **scanned/image** documents
(its `source` is an image URI, not a PDF); swap it in upstream when the loan
packet arrives as page images rather than a digital PDF.

## Required grants (tenant admin, once)

```json
{"capability_ref": "cap.sftp_login", "account_alias": "primary",
 "secrets": {"username": "...", "password": "...", "private_key": "", "private_key_passphrase": ""},
 "input_defaults": {"host": "sftp.example.com", "port": 22}}

{"capability_ref": "cap.sftp_read", "account_alias": "default", "secrets": {}, "input_defaults": {}}

{"capability_ref": "cap.doc_extract", "account_alias": "default", "secrets": {}, "input_defaults": {}}

{"capability_ref": "cap.webhook_send", "account_alias": "default", "secrets": {}, "input_defaults": {}}

{"capability_ref": "cap.file_manage", "account_alias": "default", "secrets": {}, "input_defaults": {}}
```

`cap.sftp_login` declares all four secret names; supply empty strings for the
unused auth method.

## Before importing

- Edit `remote_path` (the inbound PDF), the `validate` URL (LOS endpoint), and
  the `archive` `dst` key.
- The backend needs the `doc` extra (`pypdf`) for PDF extraction; the LLM field
  pass is optional and degrades to text-only when no model is configured.

## Stage-2 features exercised

- **Dry-run** — start with `"mode": "dry_run"` (see
  [../README.md](../README.md)). The side-effecting / undeclared nodes
  (`cap.sftp_read`, `cap.doc_extract`, `cap.webhook_send`, `cap.file_manage`)
  are **simulated** (no fetch, no POST, no archival write) while the topology
  is walked — a safe rehearsal that never validates against or writes to the
  real LOS.
- **Retention** — the archived application is a `stored_object` and the run is
  a `run` (the two erasable resource types). Set a policy with
  `PUT /retention/policies/stored_object`, freeze a document under
  investigation with `POST /retention/legal-hold`
  (`{"resource_type": "stored_object", ...}`), or honour erasure with
  `POST /retention/erase` — the archive is a governed resource, not an
  orphaned blob.
- **Tamper-evident audit** — extraction and archival are hash-chained; verify
  with `GET /audit/verify`.

Import + run: see [../README.md](../README.md).
