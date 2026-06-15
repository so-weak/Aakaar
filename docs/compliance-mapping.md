# Compliance Control → Evidence Mapping

**Purpose:** for a bank's compliance/audit function, map each control Aakaar
claims to the concrete artifact that satisfies it — the endpoint, the service
module, and (where one exists) the automated test. This is an evidence index, not
a certification. Every path, endpoint, and key below was verified against the
codebase; an entry with no automated test names the implementation module and the
manual verification step instead of inventing one.

**How to read the columns**
- **Control** — the regulatory/security requirement, in generic terms.
- **Mechanism** — how Aakaar implements it.
- **Endpoint(s)** — the API surface that exercises it (paths relative to the API
  root; routers in `aakaar/aakaar/api/routers/`).
- **Implementation** — the service/module that owns the logic.
- **Evidence (test / verification)** — automated test under `aakaar/tests/`, or a
  manual verification step.

Run the test suite that backs these claims with:

```bash
cd aakaar && ../aakaar/.venv/bin/python -m pytest tests/ -q -p no:cacheprovider
```

---

## 1. Segregation of duties (maker-checker)

| Control | Mechanism | Endpoint(s) | Implementation | Evidence |
|---------|-----------|-------------|----------------|----------|
| A sensitive change requires a second person to approve | Gated publish/run-start opens a pending `ApprovalRequest` (HTTP 202); a different admin decides | `PATCH /workflows/{workflow_id}` (publish), `POST /workflows/{workflow_id}/runs` (run-start) — both →202 when gated; `POST /approvals/{id}/approve`, `POST /approvals/{id}/reject`, `GET /approvals` | `services/governance/service.py` | `tests/test_governance_service.py::test_checker_can_approve` |
| The approver must not be the requester | `SelfApprovalError` → 409 | `POST /approvals/{id}/approve` | `services/governance/service.py` (`decide`) | `tests/test_governance_service.py::test_maker_cannot_be_checker` |
| A decision is final and durable | Re-deciding a non-pending request → 409; decision committed before the action runs | `POST /approvals/{id}/approve` | `services/governance/service.py`, `api/routers/approvals.py` | `tests/test_governance_service.py::test_cannot_decide_twice` |
| The gate is tenant-scoped | Cross-tenant request id is invisible (404) | `GET /approvals/{id}` | `api/repositories/approvals.py` | `tests/test_governance_service.py::test_cross_tenant_request_is_invisible` |

ADR: [0006](adr/0006-maker-checker-governance.md).

---

## 2. Tamper-evident audit trail

| Control | Mechanism | Endpoint(s) | Implementation | Evidence |
|---------|-----------|-------------|----------------|----------|
| Every privileged action is recorded | Tenant-scoped rows written to `audit_log` + JSONL mirror; each `record` commits in its own session | (implicit on every mutating route; mirror at `data/audit/audit.jsonl`) | `services/audit/recorder.py`, `services/audit/sink.py` | `tests/test_audit_ledger.py::test_recorder_writes_hash_chain` |
| The trail cannot be silently edited/deleted/reordered | Per-tenant sha256 hash chain (`entry_hash` over `prev_hash` + immutable fields) | `GET /audit/verify` | `services/audit/chain.py`, `services/audit/ledger.py` | `tests/test_audit_ledger.py::test_verify_detects_tampered_payload`, `::test_verify_detects_gap_from_deleted_row` |
| An auditor can verify the trail | `verify` recomputes the chain and reports first break | `GET /audit/verify`, `GET /audit/tenants/{id}/verify` (superuser) | `services/audit/ledger.py` (`verify_chain`) | `tests/test_audit_ledger.py::test_http_verify_ok` |
| An auditor can re-verify offline | `export` streams JSONL with `seq`/`prev_hash`/`entry_hash` | `GET /audit/export`, `GET /audit/tenants/{id}/export` (superuser) | `api/routers/audit.py` (`_export_lines`) | `tests/test_audit_ledger.py` (export/HTTP cases) |
| One tenant's trail can't taint another's | Chains are per-tenant | — | `services/audit/ledger.py` | `tests/test_audit_ledger.py::test_chains_are_per_tenant_isolated` |

ADR: [0007](adr/0007-tamper-evident-audit.md). **Residual:** tamper-*evident*,
not tamper-*proof* — see the [whitepaper](security-whitepaper.md) §8; pin the
chain head off-box via periodic export.

---

## 3. Records management: retention, legal hold, right-to-erasure

| Control | Mechanism | Endpoint(s) | Implementation | Evidence |
|---------|-----------|-------------|----------------|----------|
| Data is retained per a defined schedule | Per-`(tenant, resource_type)` `ttl_days` policy (null = forever) | `GET/PUT /retention/policies[/{resource_type}]` | `services/retention/service.py` (`upsert_policy`, `sweep`) | `tests/test_retention_service.py::test_sweep_erases_expired_run_and_scrubs_pii`, `::test_ttl_null_retains_forever` |
| Specific records can be preserved (litigation hold) | `legal_hold` flag; held resources skipped by sweep and refused by erasure | `POST /retention/legal-hold` | `services/retention/service.py` (`set_legal_hold`) | `tests/test_retention_service.py::test_sweep_skips_legal_hold` |
| A hold outranks an erasure request | Erasure under hold → 409 | `POST /retention/erase` | `services/retention/service.py` (`erase_resource`) | `tests/test_retention_service.py::test_explicit_erase_refused_under_legal_hold` |
| Right-to-erasure for personal data | Scrub run I/O + mirrored event payloads / delete object bytes; tombstone remains | `POST /retention/erase` | `services/retention/service.py` (`_erase_run`, `_erase_stored_object`) | `tests/test_retention_service.py::test_sweep_erases_expired_run_and_scrubs_pii`, `::test_erase_is_idempotent` |
| Erasure itself is provable | Each erasure is audited; the audit log is never an erasure target | `POST /retention/erase` → `audit_log` (`retention.erased`) | `services/retention/service.py` + `services/audit/recorder.py` | `tests/test_retention_service.py::test_erasure_preserves_and_extends_audit_trail` |

ADR: [0008](adr/0008-retention-legal-hold-erasure.md). **Operational caveat:** the
**TTL sweep is not auto-wired** into the lifespan — automatic expiry requires
scheduling `sweep_all_tenants()` externally; on-demand erasure and legal hold work
via the API today. See [operations-manual](operations-manual.md) §4.

---

## 4. Authentication, authorization, MFA

| Control | Mechanism | Endpoint(s) | Implementation | Evidence |
|---------|-----------|-------------|----------------|----------|
| Strong token signing; no algorithm confusion | RS256/JWKS with the verification algorithm **pinned** (token `alg` header never trusted); HS256 default | `POST /auth/login`, `GET /auth/.well-known/jwks.json` | `api/auth/jwt.py`, `api/routers/auth.py`, `api/routers/jwks.py` | Config: `AAKAAR_JWT_ALG`, `AAKAAR_JWT_KEY_DIR`; verify via the JWKS endpoint and a signed token. Login/role: `tests/test_api_auth.py::test_login_success`, `::test_login_wrong_password` |
| Role-based access control | `require_tenant_user` / `require_tenant_admin` / `require_superuser` dependencies | (all routers) | `api/deps.py` | `tests/test_api_auth.py::test_role_enforcement`, `::test_missing_token_rejected` |
| Multi-factor authentication (enforced) | TOTP step-up; a user with MFA on must present a token whose `amr` proves the second factor; one-time recovery codes; anti-replay | `POST /auth/login` (→ `mfa_required`), `POST /auth/mfa/verify`, MFA enroll/confirm on `/auth/mfa/*` | `api/auth/totp.py`, `api/routers/mfa.py`, `api/routers/auth.py` | Implementation modules above + config `AAKAAR_MFA_ENCRYPTION_KEY`. **No dedicated unit test in `tests/` today** — verify via the documented login→MFA flow ([README](../README.md#authentication--security)). |
| Federated SSO | OIDC authorization-code + PKCE + nonce; id_token verification; `email_verified` gating | `/auth/oidc/...` | `api/auth/oidc.py`, `api/routers/oidc.py` | Implementation modules above + config `AAKAAR_OIDC_*`. Verify against your IdP. |
| Credential-stuffing resistance | Rate limiter; dedicated `/auth` bucket | (all routes; `/auth` bucket) | `core/middleware/rate_limit.py` | Config `AAKAAR_RATE_LIMIT_ENABLED`, `AAKAAR_RATE_LIMIT_AUTH_PER_MIN` (default 20/min) |
| First-superuser bootstrap is not a standing backdoor | Bootstrap from env; rotate after first login | startup hook | `api/bootstrap.py` | Config `AAKAAR_SUPERUSER_EMAIL`/`_PASSWORD`; rotate per [SECURITY.md](../SECURITY.md). |

> **Evidence honesty note.** MFA and OIDC are implemented and wired (routers +
> `api/auth/` modules) but do **not** have dedicated automated tests under
> `aakaar/tests/` at this revision. The evidence is the implementation module
> plus the documented verification flow — do not record a test ID for them.

---

## 5. Tenant isolation / data residency

| Control | Mechanism | Endpoint(s) | Implementation | Evidence |
|---------|-----------|-------------|----------------|----------|
| One tenant cannot read another's data | App-level `tenant_id` scoping; cross-tenant id → opaque 404 | (all tenant-scoped routes) | `db/tenancy.py`, per-router resolution | `tests/test_api_workflows.py`, `tests/test_api_run_lifecycle.py`, `tests/test_api_admin_users.py` (cross-tenant 404 cases) |
| DB-level isolation (where supported) | Postgres RLS via `app.tenant_id` GUC; `aakaar_app` non-superuser role; fail-closed strict mode | — | `db/session.py`, `extras/rls/setup_app_role.sql` | Config `AAKAAR_RLS_STRICT`; verify by connecting as `aakaar_app` and attempting a cross-tenant read |
| Data residency | All state under one `data_dir` on a host you control; air-gappable | — | `core/config.py` (`AAKAAR_DATA_DIR`), `docker-compose.airgap.yml` | ADR [0005](adr/0005-airgap-posture.md); pin the host's location |

ADR: [0003](adr/0003-app-level-tenancy-optional-rls.md). **Residual:** on SQLite,
isolation is application-level only; filesystem access to `aakaar.sqlite` defeats
it — host hardening is in scope.

---

## 6. Secrets management

| Control | Mechanism | Endpoint(s) | Implementation | Evidence |
|---------|-----------|-------------|----------------|----------|
| Secrets are not stored in the database | Grants hold names only; values in the filesystem vault (mode 0600); API never returns values | (grants on `/workflows` / admin surfaces) | `vault/local.py`, `interpreter/credentials.py` | Code review; the schema has no secret-value columns ([CONTRIBUTING.md](../CONTRIBUTING.md) secrets discipline) |
| Secrets are encrypted at rest | Fernet via a pluggable `KeyProvider`; fail-closed option | — | `vault/key_provider.py`, `vault/local.py` | `tests/test_vault_encryption.py`; config `AAKAAR_VAULT_KEY`, `AAKAAR_VAULT_REQUIRE_ENCRYPTION` |
| Keys can be rotated without downtime | MultiFernet window (first encrypts, rest decrypt) | — | `vault/key_provider.py` | `tests/test_vault_encryption.py` (rotation cases); [runbook 03](../runbooks/03-vault-key-rotation.md) |
| External root of trust (KMS/HSM) | `EnvelopeKeyProvider` scaffold (envelope encryption, injected unwrap) | — | `vault/key_provider.py` (`EnvelopeKeyProvider`) | ADR [0004](adr/0004-keyprovider-kms-seam.md); unit-tested with a fake unwrap |
| Secrets don't leak into derived state | Credential-shaped keys redacted from run outputs, checkpoints, audit payloads | — | `interpreter/durability.py` (`redact_env`), `services/audit/recorder.py` | `tests/test_audit_ledger.py` / `tests/test_durability_resume_dryrun_hitl.py` (redaction asserts) |

---

## 7. Execution integrity & safe automation

| Control | Mechanism | Endpoint(s) | Implementation | Evidence |
|---------|-----------|-------------|----------------|----------|
| A restart never re-fires a completed side effect | Durable per-layer checkpoints; resume skips completed nodes | (startup recovery) | `interpreter/durability.py`, `interpreter/orchestrator.py` | `tests/test_durability_resume_dryrun_hitl.py::test_resume_skips_completed_nodes_no_redispatch` |
| A poison run can't loop forever on resume | Resume cap | — | `core/config.py` (`AAKAAR_MAX_RUN_RESUMES`), orchestrator | `tests/test_durability_resume_dryrun_hitl.py::test_recover_respects_resume_cap` |
| A workflow can be rehearsed without real side effects | Dry-run simulates side-effecting capabilities (`side_effecting` flag; undeclared treated as side-effecting) | — | `interpreter/executor.py`, `shared/registry/types.py` | `tests/test_durability_resume_dryrun_hitl.py::test_dry_run_simulates_side_effecting_runs_readonly`, `::test_dry_run_simulates_undeclared_capability` |
| Outbound HTTP can't reach internal hosts (SSRF) | Private/loopback/link-local blocked unless allowlisted per call | — | `core/net/ssrf.py` (`assert_host_allowed`, guarded httpx clients) | unit tests for the SSRF guard; see [capability-authoring guide](capability-authoring-guide.md) |
| Arbitrary worker-filesystem reads are off by default | `file.read_local` gated behind a flag | — | `interpreter/activities/file.py` | Config `AAKAAR_ALLOW_LOCAL_PATHS` (unset = denied) |
| Run lifecycle is operator-controllable & audited | Pause/resume/cancel/rerun, all audited | `POST /runs/{id}/pause|resume|cancel|rerun` | `api/routers/runs.py` | `tests/test_api_run_lifecycle.py` |

---

## 8. Privacy of captured automation

| Control | Mechanism | Endpoint(s) | Implementation | Evidence |
|---------|-----------|-------------|----------------|----------|
| Captured recordings never contain raw keystrokes | Agent emits only an allowlisted hotkey set as `key` events; all other input becomes a `text` count; server rejects violations | `POST /recordings`, `POST /recordings/{id}/stop` | `aakaar-agent/.../capabilities/activity_recording.py`, `services/recordings/compiler.py`, `services/recordings/service.py` | `tests/test_recordings_compiler.py`, `tests/test_recordings_service.py`; agent: `aakaar-agent/tests/test_activity_recording.py` |
| Typed secrets don't reach the compiled draft | `<REPLACE_REDACTED_TEXT_n>` placeholders in `cap.desktop_type` nodes | (compiled at stop) | `services/recordings/compiler.py` | `tests/test_recordings_compiler.py` |

---

## Quick verification checklist for an audit

```bash
# 1. Prove the audit trail is intact for each tenant (and pin the head off-box):
curl -s -H "Authorization: Bearer $SU" http://localhost:8000/audit/tenants/$TID/verify
curl -s -H "Authorization: Bearer $SU" http://localhost:8000/audit/tenants/$TID/export > audit-$TID.jsonl

# 2. Show maker-checker is enforced (self-approval rejected): expect 409.
# 3. Show a legal hold blocks erasure: POST /retention/legal-hold then /retention/erase -> 409.
# 4. Run the controls test suite:
cd aakaar && ../aakaar/.venv/bin/python -m pytest \
  tests/test_governance_service.py tests/test_audit_ledger.py \
  tests/test_retention_service.py tests/test_vault_encryption.py \
  tests/test_durability_resume_dryrun_hitl.py -q -p no:cacheprovider
```
