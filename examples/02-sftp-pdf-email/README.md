# 02 — SFTP fetch → PDF extract → email

Fetches a PDF over an authenticated SFTP session into managed storage,
extracts its text with `cap.pdf_extract`, and emails the text with the
original PDF attached.

```
login (cap.sftp_login) → fetch (cap.sftp_read) → extract_text (cap.pdf_extract) → send (cap.email_send)
```

## How credentials flow

The DAG never contains a hostname, username, or password. Each credentialed
node carries only `account_alias`; at run time the executor resolves the
alias against the tenant's **grant**, injects the grant's `input_defaults`
(host, port, …) into unset inputs, and the capability fetches the secret
values from the vault. Rotating a credential is a grant update — the
workflow is untouched.

- **login** publishes its outputs as `sftp` (`outputs_as`), so downstream
  nodes write `${sftp.session}`.
- **fetch** streams `remote_path` into the object store and returns
  `{uri, filename, size}`. The 3-attempt retry absorbs transient SFTP drops.
- **extract_text** reads the stored PDF (`pypdf` — the backend's `doc`
  extra); `max_pages: 10` caps the volume and sets `truncated` instead of
  failing on longer documents. Password-protected PDFs fail the node with a
  clear error.
- **send** uses the whole extracted text as the body (a ref must be the
  entire string — no embedding) and re-attaches the stored PDF by URI.

## Required grants (tenant admin, once)

```json
{"capability_ref": "cap.sftp_login", "account_alias": "primary",
 "secrets": {"username": "...", "password": "...", "private_key": "", "private_key_passphrase": ""},
 "input_defaults": {"host": "sftp.example.com", "port": 22}}

{"capability_ref": "cap.sftp_read", "account_alias": "default", "secrets": {}, "input_defaults": {}}

{"capability_ref": "cap.pdf_extract", "account_alias": "default", "secrets": {}, "input_defaults": {}}

{"capability_ref": "cap.email_send", "account_alias": "primary",
 "secrets": {"username": "reports@example.com", "smtp_password": "..."},
 "input_defaults": {"host": "smtp.example.com", "port": 587, "use_starttls": true,
                    "from_addr": "reports@example.com",
                    "recipient_allowlist": ["@example.com"]}}
```

Notes:

- `cap.sftp_login` declares all four secret names; supply empty strings for
  the unused auth method (the grant API requires the exact declared set).
- `recipient_allowlist` (exact addresses and/or `@domain` entries) hard-caps
  where this grant can send, enforced before any SMTP connection — keep it.

## Before importing

- Edit `remote_path` and the `to` recipient.
- The recipient must be allowed by your grant's `recipient_allowlist`.

Import + run: see [../README.md](../README.md).
