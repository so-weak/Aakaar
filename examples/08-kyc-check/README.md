# 08 — KYC sanctions/PEP screening and record

Gathers a customer's KYC profile from the customer system, screens the identity
against a sanctions/PEP provider through the SSRF guard, and records the
outcome to the compliance system of record. Every node is written to the
tamper-evident audit ledger, so the screening is independently provable after
the fact.

```
stamp (time.now) ──────────────────────────────┐
gather (cap.api_call) → screen (cap.webhook_send) → record (cap.webhook_send)
       └──────────────────────────────────────────────────────┘
```

## How it works

- **gather** GETs the customer's KYC profile from the CIF/customer system with
  `cap.api_call`, authenticated by the grant alias and SSRF-guarded; retries
  absorb transient 5xx.
- **screen** POSTs the customer identity to the screening provider with
  `cap.webhook_send`. The guard resolves the host and **blocks any target that
  maps to a private / loopback / link-local address** unless that exact host is
  named in `allow_hosts` — so a misconfigured or hostile URL can't be used to
  reach internal services. The provider's HTTP status and body come back as
  `${screen.status}` / `${screen.body}`.
- **record** POSTs the screening status, the provider response, and the
  customer profile to the compliance case store (again SSRF-guarded). Each
  `${...}` is the whole value for its payload key (refs cannot be embedded
  inside a larger string).

`gather` and `record` both fan in to give `record` the customer profile and the
screening result together; `stamp` supplies a stable `${stamp.utc_datetime}` to
both outbound calls.

## "Record" = the tamper-evident audit ledger

Beyond the explicit `record` webhook to your case store, the platform writes
**every** node execution to a hash-chained audit ledger automatically. That is
the durable, independent compliance record:

- `GET /audit/verify` recomputes the chain and proves nothing was altered.
- `GET /audit/export` streams the chain in order for an examiner / SIEM.

So the screening is provable even if the downstream case store is later
edited — the chain is the source of truth for *what the platform did*.

## Reaching an internal screening service

If your sanctions provider is on the internal network (a private address), add
its exact hostname to `allow_hosts` on the `screen` (and/or `record`) node:

```json
"allow_hosts": ["screening.internal.bank"]
```

Everything else private stays blocked. Leave `allow_hosts` unset for public
endpoints.

## Required grants (tenant admin, once)

```json
{"capability_ref": "cap.api_call", "account_alias": "primary",
 "secrets": {"token": "...", "api_key": "", "username": "", "password": ""},
 "input_defaults": {}}

{"capability_ref": "cap.webhook_send", "account_alias": "default", "secrets": {}, "input_defaults": {}}
```

`cap.api_call` declares four secret names (token / api_key / username /
password); supply the one your CIF API uses and empty strings for the rest.
`cap.webhook_send` holds no credentials — pass any provider auth token via the
node's `headers` (values are never logged). `time.now` is built-in.

## Before importing

- Edit the customer URL (`gather`), the screening endpoint (`screen`), and the
  compliance case endpoint (`record`).
- For an internal provider, set `allow_hosts` as above and pass its auth token
  via `headers` on the `screen` node.

## Stage-2 features exercised

- **SSRF guard** — both outbound calls go through `cap.api_call` /
  `cap.webhook_send`, which block private-address targets by default.
- **Tamper-evident audit (record)** — the screening is hash-chained; verify
  with `GET /audit/verify`, export with `GET /audit/export`.
- **Dry-run** — `"mode": "dry_run"` simulates the (undeclared / side-effecting)
  calls so you can rehearse the topology without contacting the provider.
- **Retention** — each run is an erasable `run` resource:
  `PUT /retention/policies/run` to set a TTL, `POST /retention/legal-hold` to
  freeze one under investigation.

Import + run: see [../README.md](../README.md).
