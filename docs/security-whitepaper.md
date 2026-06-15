# Aakaar Security Whitepaper

**Audience:** a regulated bank's security, risk, and architecture reviewers.
**Scope:** the platform (`aakaar/` API, planner, interpreter, capabilities,
vault, scheduler, remote dispatch), the operator SPA (`aakaar-web/`), the
workstation agent (`aakaar-agent/`), the optional rendezvous broker
(`aakaar-broker/`), and the MCP projection (`aakaar-mcp/`). The sample tenant
apps (`admin-app/`, `nbbl-app/`) are *targets* of automation, not part of the
platform's trust boundary.

This document describes the trust model and the controls that implement it.
Every configuration key named here is read in
[`aakaar/aakaar/core/config.py`](../aakaar/aakaar/core/config.py); every endpoint
named here exists in `aakaar/aakaar/api/routers/`. For the reporting process and
the deployment hardening checklist, see [SECURITY.md](../SECURITY.md). For the
control-to-evidence mapping, see [compliance-mapping.md](compliance-mapping.md).

---

## 1. Trust model

Aakaar stores tenant credentials, executes LLM-planned workflows against
external systems, and can dispatch GUI automation to remote workstations. The
trust boundary is drawn as follows.

| Principal | Trusted to | NOT trusted to |
|-----------|-----------|----------------|
| **Platform host / filesystem** | Hold the SQLite store, vault, object store, audit mirror. Treated as fully trusted; its OS hardening is part of the boundary. | — (anyone with filesystem access reads every tenant's data — see §2) |
| **Tenant admin** | Manage their own tenant: users, grants, retention, approvals, agents. | Reach another tenant; bypass maker-checker by self-approving. |
| **Tenant user (operator)** | Author/run workflows within granted capabilities. | Mint capabilities; read secret *values*; reach another tenant. |
| **The LLM planner** | Propose a DAG from a prompt. | Invent capabilities, reach credentials, or escape registry/grant validation — its output is **untrusted input** (§7). |
| **Remote agent** | Execute the nodes the API dispatches to it, after DB-verified per-agent auth. | Initiate inbound connections to the platform; act for another tenant. |
| **Rendezvous broker** (optional) | Relay frames between agent and API. | Verify or trust agent keys — it forwards them opaquely; the **API** is authoritative (§3.1). The broker host is trusted infrastructure. |

Defaults are backward-compatible and conservative; the hardened modes (RS256,
MFA enforcement, OIDC, RLS, vault-require-encryption, maker-checker) are opt-in
via configuration and workflow policy.

---

## 2. Tenant isolation

**Primary guard — application-level scoping on SQLite.** Every tenant-owned row
carries a `tenant_id`. The session layer (`aakaar/aakaar/db/tenancy.py`) scopes
queries to the caller's tenant, and routers resolve resources only within that
tenant: a cross-tenant id reads as an **opaque 404**, so existence cannot be
probed. System actors (login, scheduler, superuser) run under an explicit
system scope. (ADR [0003](adr/0003-app-level-tenancy-optional-rls.md).)

**Defense-in-depth — optional Postgres RLS.** When `AAKAAR_DB_URL` points at
Postgres and the app connects as the non-superuser, table-owning `aakaar_app`
role (`extras/rls/setup_app_role.sql`), the tenancy scope is mirrored into a
transaction-local `app.tenant_id` GUC that RLS policies read.
`AAKAAR_RLS_STRICT=true` makes the no-scope case deny-all (fail closed). On
SQLite this layer is a no-op and the application guard stands alone.

**Residual the reviewer must accept.** On SQLite there is no database-level
enforcement: anyone with filesystem read access to `data/aakaar.sqlite` can read
every tenant's metadata, and anyone with the vault directory can attempt to read
secrets (subject to §5 encryption). Host hardening and file permissions are
therefore inside the trust boundary, not outside it.

---

## 3. Agent authentication and dispatch

Remote agents run capability nodes on a workstation (desktop/GUI RPA). The
direction of trust is deliberate: **the platform never opens inbound connections
to a workstation.** The agent dials *out* to `/ws/agents`.

- **Enrollment** (`POST /agents/enroll`, tenant-admin only) mints a one-time key
  of the form `<agent_id>.<secret>` where the secret is a
  `secrets.token_urlsafe(32)` value. The server stores **only a bcrypt hash** of
  the secret — the cleartext key is shown once and never persisted.
- **Connection** carries the key in the `x-agent-key` header; the API verifies it
  against the stored hash and pins the session to the verified agent's tenant.
- **Revocation** (`DELETE /agents/{id}`) invalidates the key immediately; a
  compromised agent is cut off by deleting it.
- Remote execution can be disabled entirely with
  `AAKAAR_REMOTE_EXEC_ENABLED=false`, which closes the `/ws/agents` surface — set
  this on deployments that use no remote agents.

### 3.1 The broker is not a trust principal

The optional rendezvous broker (`aakaar-broker/`) lets an agent and the API meet
at a third process when neither has a stable address. **The broker forwards the
`x-agent-key` opaquely in its `open` envelope and never verifies agent
credentials — the API performs the authoritative DB check, exactly as for a
direct connection.** The relay code states this explicitly and pins each relayed
session to the DB-verified key's tenant.

The honest residual: the broker process necessarily handles the key in cleartext
while relaying, so a **hostile broker operator could capture keys or forge `data`
frames on sessions it relays** (the API still pins each session to the verified
tenant). The broker host is therefore *trusted infrastructure*, and the broker
itself is fail-closed:

- `AAKAAR_BROKER_TOKEN` is **required with no default** — the broker refuses to
  start without it.
- On the API side, setting `AAKAAR_BROKER_URL` makes `AAKAAR_BROKER_TOKEN`
  required there too (startup fails closed); both sides must hold the same token.
- `AAKAAR_BROKER_MAX_SESSIONS` (default 200) and
  `AAKAAR_BROKER_HANDSHAKE_TIMEOUT` (default 10s) bound the blast radius.

Outage handling and the direct-dial fallback:
[runbooks/05-broker-outage.md](../runbooks/05-broker-outage.md).

---

## 4. Recording redaction (privacy by construction)

The agent-side activity recorder (`cap.activity_recording`) is how an operator
captures a desktop interaction to compile into a workflow draft. Its **hard
privacy rule is enforced in the agent process: raw keystrokes never leave it.**

- Only an exact allowlist of navigation/hotkey combos is emitted as `key`
  events: `enter, tab, esc, ctrl+a, ctrl+c, ctrl+v, ctrl+s, ctrl+tab, alt+tab,
  shift+tab` (`_KEY_ALLOWLIST`; on macOS `cmd` is normalised to `ctrl`).
- **Every other key press** (printable characters, anything not on the
  allowlist) is aggregated into `text` events that carry only a **count** — never
  the characters.
- The server-side compiler turns those into `cap.desktop_type` nodes whose value
  is a `<REPLACE_REDACTED_TEXT_{n}>` placeholder
  (`services/recordings/compiler.py`), so a typed password or account number is
  never present in the compiled draft; the operator fills the intended text in
  later.

The server rejects a capture that violates the contract, so a compromised or
buggy agent cannot smuggle raw text through. Recording slots self-expire and are
reclaimed, so a crashed server can't permanently wedge an agent.

---

## 5. Secrets lifecycle

**Secrets never live in the database.** Capability grants store secret *names*
(`SecretSpec.name`); values go to the filesystem vault
(`data/vault/<tenant_id>/...`, files mode 0600). The API never returns secret
values, and a capability handler fetches credentials fresh per call from the
vault by `account_alias`, validated against the tenant's grant for that
capability (`aakaar/aakaar/interpreter/credentials.py`); the fetched value is
never written back into the DAG env.

**Encryption at rest.** The vault is Fernet-encrypted when a key is configured.
Key material is supplied through a pluggable `KeyProvider` seam
(ADR [0004](adr/0004-keyprovider-kms-seam.md)):

- `AAKAAR_VAULT_KEY` — comma-separated Fernet keys. The **first** key encrypts
  every new write; the rest are retired keys retained for decryption during a
  rotation window (MultiFernet semantics).
- `AAKAAR_VAULT_REQUIRE_ENCRYPTION=1` — **fail closed**: refuse to start if no key
  is configured, instead of falling back to plaintext (which only logs a
  warning). Set this everywhere outside dev.
- **External KMS** without a KMS dependency: `EnvelopeKeyProvider` is a scaffold
  for envelope encryption — a data key wrapped by a master key the bank's KMS/HSM
  holds and never releases, with the unwrap injected as a callable so the
  platform imports no cloud SDK. The unwrapped data key lives in process memory
  for the vault's lifetime (the documented residual).

**Rotation** is a documented procedure (add the new key first, re-encrypt, drop
the old): [runbooks/03-vault-key-rotation.md](../runbooks/03-vault-key-rotation.md).

**Other secret-shaped material.** Run outputs, checkpoint env snapshots, and
audit payloads are redacted for credential-shaped keys
(`password`/`token`/`api_key`/`secret`/`authorization`, plus `totp_secret` for
audit) before they are persisted — so a checkpoint or an audit row never carries
a secret. TOTP secrets are optionally encrypted at rest with
`AAKAAR_MFA_ENCRYPTION_KEY`.

---

## 6. Identity, tokens, and MFA

Auth is defense-in-depth; defaults are HS256 + app-layer tenancy, hardened modes
are opt-in (full sequence diagrams in the [root README](../README.md#authentication--security)).

- `AAKAAR_JWT_SECRET` is **required** — the server refuses to start without it.
- **RS256/JWKS** (`AAKAAR_JWT_ALG=RS256`, `AAKAAR_JWT_KEY_DIR`): RSA keys with a
  `kid` header; every public key is published at
  `GET /auth/.well-known/jwks.json` so tokens survive a rotation. The verification
  algorithm is **pinned** — the token's own `alg` header is never trusted (no
  `alg:none`, no HS↔RS confusion). This is the single most important auth
  hardening.
- **MFA (TOTP)** is *enforced*, not just offered: a user with MFA enabled must
  present a token whose `amr` proves the second factor. Anti-replay on the
  time-step; one-time recovery codes; optional secret encryption at rest.
- **OIDC/SSO** (`AAKAAR_OIDC_ENABLED=true`): authorization-code flow with PKCE +
  nonce, id_token verification (asymmetric-alg allowlist, `aud`/`iss`,
  `userinfo.sub == id_token.sub`), `email_verified` gating.
- **Rate limiting** is on by default (`AAKAAR_RATE_LIMIT_ENABLED=true`); the
  `/auth` bucket (`AAKAAR_RATE_LIMIT_AUTH_PER_MIN`, default 20/min) blunts
  credential stuffing.

---

## 7. The LLM is untrusted input

The planner turns a natural-language prompt into a DAG, but **it cannot expand
the platform's authority**. Planner output is validated against the registry
schemas (strict Pydantic, `extra="forbid"`) and the tenant's capability grants
before a DAG can be saved or run. A model cannot:

- invent a capability that isn't in the registry,
- reference a capability the tenant lacks a grant for,
- reach a credential directly (handlers fetch from vault, the LLM never sees the
  value), or
- produce inputs that don't match a capability's declared schema.

Prompt injection that tries to make the model "do something else" is bounded by
this: the worst a poisoned prompt can compose is a DAG of *already-granted*
capabilities, which is exactly what the operator could run by hand — and, for
sensitive workflows, still subject to maker-checker (§8) and dry-run rehearsal.

---

## 8. Governance, audit, and the RPA attack surface

**Maker-checker (segregation of duties).** Publishing a workflow version and
starting a run of a sensitive workflow can be gated
(`requires_approval=True` or `sensitivity='elevated'`). A gated action returns
**202** with a pending `ApprovalRequest`; a *different* tenant admin decides it
at `POST /approvals/{id}/approve|reject`. The approver may not be the requester
(`SelfApprovalError` → 409). ADR [0006](adr/0006-maker-checker-governance.md).

**Tamper-evident audit.** Tenant-scoped audit rows form a per-tenant sha256 hash
chain; `GET /audit/verify` recomputes it and `GET /audit/export` streams it for
**offline** re-verification. Tamper-*evident*, not tamper-*proof*: a writer with
SQLite access could forge a self-consistent chain, so exporting/attesting the
chain head off-box is recommended. ADR
[0007](adr/0007-tamper-evident-audit.md).

**RPA-agent attack surface.** A workstation agent is the highest-consequence
component (it drives a real desktop with real credentials). The surface and its
mitigations:

| Surface | Mitigation |
|---------|-----------|
| Agent key theft | One-time key, bcrypt-hashed server-side, per-agent, revocable via `DELETE /agents/{id}`. |
| Inbound compromise of a workstation | Platform never dials in; the agent dials out only. |
| Malicious/buggy agent leaking typed secrets | Server enforces the recording-redaction contract (§4) and rejects violations. |
| A run sending money twice after a restart | Durable per-layer checkpoints skip completed side-effecting nodes (ADR 0002). |
| An untested DAG firing real side effects | The engine's **dry-run** path simulates side-effecting capabilities (`side_effecting` flag); see the [capability-authoring guide](capability-authoring-guide.md). |
| Outbound HTTP from a capability hitting internal hosts (SSRF) | `assert_host_allowed` / SSRF-guarded httpx clients block private/loopback/link-local targets unless explicitly allowlisted per call (`aakaar/aakaar/core/net/ssrf.py`). |
| Arbitrary worker-filesystem reads | `file.read_local` is **off by default**; `AAKAAR_ALLOW_LOCAL_PATHS=true` is required to enable it and should stay unset on shared hosts. |

### Compromise response (RPA agent)

1. **Contain.** `DELETE /agents/{id}` to revoke the key; or set
   `AAKAAR_REMOTE_EXEC_ENABLED=false` to close `/ws/agents` fleet-wide.
2. **Stop in-flight work.** Pause/cancel affected runs
   (`POST /runs/{id}/pause|cancel`); see
   [runbooks/07-run-stuck-or-paused.md](../runbooks/07-run-stuck-or-paused.md).
3. **Assess blast radius.** `GET /audit/verify` to confirm the trail is intact,
   then review `action`-filtered audit (`run.start`, `run.cancel`,
   `approval.approve`, …) for what the agent did and which capabilities/secrets
   it could reach.
4. **Rotate.** Rotate any vault secrets the agent's grants exposed
   ([runbook 03](../runbooks/03-vault-key-rotation.md)); re-enroll the agent with
   a fresh key.
5. **Preserve.** Place affected runs under legal hold
   (`POST /retention/legal-hold`) so investigation records are exempt from
   retention erasure.

Fleet-level degradation and reconnect storms:
[runbooks/04-agent-fleet-degradation.md](../runbooks/04-agent-fleet-degradation.md).

---

## 9. Network posture

- `AAKAAR_CORS_ORIGINS` — exact origins of the operator UI only (the default
  allows the Vite dev server; replace in production). Allow-credentials is off
  (Bearer token in a header, not a cookie).
- Bind deliberately: `dev.sh` defaults the API to `0.0.0.0` so LAN agents can
  reach it; set `AAKAAR_API_HOST=127.0.0.1` (or firewall the port) when no remote
  agents are used.
- `AAKAAR_OPENAI_TLS_VERIFY` stays `true` unless pointing at a self-signed *local*
  LLM gateway via `AAKAAR_OPENAI_BASE_URL`; it logs loudly when disabled and must
  never be `false` against a public endpoint.
- Air-gapped operation is first-class (ADR
  [0005](adr/0005-airgap-posture.md)): `AAKAAR_HF_OFFLINE=true`, no-LLM boot,
  local-only audit/metrics, and `docker-compose.airgap.yml`.

---

## 10. Summary of residual risks (accept-or-mitigate)

| Residual | Why it exists | Mitigation / acceptance |
|----------|---------------|-------------------------|
| Filesystem access = all-tenant read on SQLite | No DB-level RLS on SQLite (ADR 0001/0003) | Host hardening + permissions; Postgres + RLS where required. |
| Audit is tamper-*evident*, not tamper-*proof* | Hash chain in the same SQLite a writer could edit | Permissions; periodic off-box export/attestation of the chain head. |
| Envelope data key in process memory | Vault must use the key in-process | Envelope protects at-rest, not against memory compromise; host hardening. |
| Hostile broker operator can capture keys / forge frames | Broker relays cleartext (§3.1) | Treat broker host as trusted infra; API still pins tenant; prefer direct dial where possible. |
| TTL retention sweep not auto-wired | `sweep_all_tenants` exists but isn't a lifespan task (ADR 0008) | Schedule it externally; on-demand erasure + legal hold work today. |
