# 06 — Dispute intake (maker-checker)

Pulls a card-dispute case from the case-management API, asks an analyst to
confirm the provisional credit in the run chat, and posts the resolution to the
core banking webhook. The workflow is marked **`requires_approval` +
`sensitivity: "elevated"`**, so it exercises the maker-checker governance gate:
**starting a run does not launch it — it opens an approval request a second
admin must approve.**

```
stamp (time.now) ───────────────────────────────────────────┐
fetch_case (cap.api_call) → confirm_credit (human.prompt) ─→ post_resolution (cap.webhook_send)
```

## Two governance layers

1. **Maker-checker gate at run-start (workflow-level).** Because
   `requires_approval` and `sensitivity` are set on the workflow,
   `workflow_is_gated(...)` is true. `POST /workflows/{id}/runs` therefore
   returns **`202 Accepted`** with an `approval` body (an `approval_request`)
   instead of `201` + a run — nothing executes yet. A **different** tenant
   admin (the checker, who may not be the maker) then calls
   `POST /approvals/{request_id}/approve`, which records the decision **and**
   starts the run, attributed to the original requester. The maker approving
   their own request is rejected (`409`, segregation of duties). These same two
   fields also gate **publishing** a new version of the workflow.
2. **In-flight human confirmation (run-level).** Inside the run,
   `confirm_credit` is a `human.prompt` control (`expects: "confirm"`) that
   pauses execution and surfaces in the run console / `pending_prompts`; the
   analyst's answer flows on as `${confirm_credit.response}`.

## How it works

- **fetch_case** GETs the dispute case from the case system with
  `cap.api_call`, authenticated by the grant alias (bearer / API key / basic,
  whatever the grant holds) and SSRF-guarded. Retries absorb transient 5xx.
- **confirm_credit** pauses for the analyst. The run sits in a waiting state
  until they respond via `POST /runs/{run_id}/respond` (or the console); the
  control node always runs on the server and is never retried.
- **post_resolution** POSTs the case, the analyst decision, and the action to
  the core endpoint through the SSRF guard. Each `${...}` is the entire value
  for its payload key (refs cannot be embedded in a larger string).

## Required grants (tenant admin, once)

```json
{"capability_ref": "cap.api_call", "account_alias": "primary",
 "secrets": {"token": "...", "api_key": "", "username": "", "password": ""},
 "input_defaults": {}}

{"capability_ref": "cap.webhook_send", "account_alias": "default", "secrets": {}, "input_defaults": {}}
```

`cap.api_call` declares four secret names (token / api_key / username /
password); supply the one your case API uses and empty strings for the rest —
the grant API requires the exact declared set. `human.prompt` and `time.now`
are built-in primitives and need no grant.

## Marking a workflow gated

The two fields live on the **workflow create body**, not inside `dag` — they
are already set in `workflow.json`:

```json
{ "name": "...", "requires_approval": true, "sensitivity": "elevated", "dag": { ... } }
```

`POST /workflows/{id}` (owner) can also flip them on an existing workflow. With
either set, both run-start and publish are gated; the default
(`requires_approval: false`, `sensitivity: "normal"`) preserves today's
ungated behaviour.

## Before importing

- Edit the case URL (`fetch_case`) and the resolution endpoint
  (`post_resolution`).
- You need **two** tenant admins to see the gate end-to-end: one to start
  (maker), one to approve (checker).

## Stage-2 features exercised

- **Maker-checker** — the headline feature, via `requires_approval` +
  `elevated` (`POST /workflows/{id}/runs` → 202; `POST /approvals/{id}/approve`
  by a second admin performs the run; self-approval → 409).
- **Human-in-the-loop** — `human.prompt` for the in-flight analyst
  confirmation.
- **Tamper-evident audit** — the gate decision, the run-start, and every node
  are hash-chained; verify with `GET /audit/verify`, stream with
  `GET /audit/export`.

Import + run: see [../README.md](../README.md).
