# Security Policy

Aakaar stores tenant credentials, executes LLM-planned workflows against
external systems, and dispatches GUI automation to remote workstations.
Security reports get priority handling.

## Reporting a vulnerability

**Do not open a public GitHub issue for a security problem.**

Email **meet.ravinandan.me@gmail.com** with the subject `[security] <summary>`
and include:

- what the vulnerability is and what an attacker gains;
- reproduction steps or a proof of concept;
- the commit SHA you tested against;
- any mitigation you already see.

You will get an acknowledgement within 2 business days and a triage verdict
within 5. We coordinate disclosure timelines case by case (default request:
90 days) and credit reporters unless they prefer otherwise.

### Scope

In scope: the `aakaar/` backend (API, planner, interpreter, capabilities,
vault, scheduler, remote dispatch), `aakaar-web/`, `aakaar-agent/`,
`aakaar-broker/`, `aakaar-capabilities/`, `aakaar-mcp/`, and the compose
files at the repo root.
Specifically interesting: tenant-isolation bypasses, capability-grant bypasses,
secret exfiltration (vault, recordings, logs), SSRF-guard escapes, agent
auth/dispatch abuse, and audit-trail tampering.

Out of scope: vulnerabilities purely in third-party dependencies (report
upstream; we pull patches), self-XSS, unauthenticated volumetric DoS without
amplification, and anything in the sample tenant apps (`admin-app/`,
`nbbl-app/`) — those simulate *targets* of automation, not the platform.

## Trust model

- **Tenant isolation is application-level on SQLite.** Every row carries a
  `tenant_id`; the session layer (`aakaar/db/tenancy.py`) scopes queries, and
  routers resolve resources only within the caller's tenant (cross-tenant ids
  read as 404). There is no database-level enforcement on SQLite — on Postgres
  deployments, row-level security adds a second layer (`extras/rls/`,
  `AAKAAR_RLS_STRICT`). Treat anyone with filesystem access to
  `data/aakaar.sqlite` as having read access to every tenant's metadata.
- **Secrets never live in the database.** Capability grants store secret
  *names*; values go to the filesystem vault (`data/vault/...`, files mode
  0600), Fernet-encrypted at rest when `AAKAAR_VAULT_KEY` is set. The API
  never returns secret values.
- **Agents authenticate with per-agent keys.** Enrollment
  (`POST /agents/enroll`, tenant-admin only) mints a one-time key of the form
  `<agent_id>.<secret>`; the server keeps only a bcrypt hash. The agent dials
  *out* to `/ws/agents` — the platform never opens inbound connections to
  workstations. Revoking the agent (`DELETE /agents/{id}`) invalidates the key.
- **Activity recording is privacy-preserving by construction.** The agent-side
  recorder (`cap.activity_recording`) never ships raw keystrokes: only an
  exact allowlist of navigation/hotkey combos (enter, tab, esc, ctrl+a/c/v/s,
  ctrl+tab, alt+tab, shift+tab) appears as `key` events; everything else is
  aggregated into `text` events carrying only a **count**. The server rejects
  the whole capture if an agent violates that contract, and the compiled draft
  workflow contains `<REPLACE_REDACTED_TEXT_n>` placeholders instead of typed
  text.
- **Outbound HTTP is SSRF-guarded.** `cap.webhook_send` and friends resolve
  hosts through `aakaar/core/net/ssrf.py`; private/loopback/link-local
  addresses are blocked unless a host is explicitly allowlisted per call.
- **The LLM is untrusted input.** Planner output is validated against the
  registry schemas and the tenant's grants before a DAG can be saved or run;
  a model cannot invent capabilities or reach credentials directly.

## Governance & compliance controls

These are the regulated-bank controls and where to exercise them. Full
control→evidence mapping (with the endpoint, service module, and test that backs
each claim) is in [docs/compliance-mapping.md](docs/compliance-mapping.md).

- **Maker-checker (segregation of duties).** A gated publish or run-start opens a
  pending approval (HTTP 202) instead of acting; a *different* tenant admin
  decides it at `POST /approvals/{id}/approve|reject`. The approver may not be the
  requester (409). Gating is per-workflow (`requires_approval` /
  `sensitivity='elevated'`). See [ADR 0006](docs/adr/0006-maker-checker-governance.md).
- **Tamper-evident audit.** Tenant-scoped audit rows form a per-tenant sha256 hash
  chain. Verify with `GET /audit/verify` (tenant admin) or
  `GET /audit/tenants/{id}/verify` (superuser); export for offline re-verification
  with `GET /audit/export`. It is tamper-*evident*, not tamper-*proof* — pin the
  chain head off-box via periodic export. See [ADR 0007](docs/adr/0007-tamper-evident-audit.md).
- **Retention, legal hold, right-to-erasure.** Manage at `/retention`
  (`GET/PUT /retention/policies`, `POST /retention/legal-hold`,
  `POST /retention/erase`). A legal hold outranks an erasure request (409). **The
  TTL sweep is not auto-wired into the lifespan** — automatic expiry requires
  scheduling `sweep_all_tenants()` externally; on-demand erasure and legal hold
  work today. See [ADR 0008](docs/adr/0008-retention-legal-hold-erasure.md).
- **Durable execution.** A restart never re-fires a completed side-effecting node
  (per-layer checkpoints); a dry-run simulates side-effecting capabilities. See
  [ADR 0002](docs/adr/0002-in-process-executor-durable-resume.md) and the
  [capability-authoring guide](docs/capability-authoring-guide.md).

## Deployment hardening checklist

Work through this before exposing an instance beyond localhost. All keys are
read in `aakaar/aakaar/core/config.py`.

### Identity and tokens

- [ ] `AAKAAR_JWT_SECRET` — long random value, unique per environment. The
      server refuses to start without it; never reuse the dev throwaway.
- [ ] Prefer asymmetric signing in production: `AAKAAR_JWT_ALG=RS256` with
      `AAKAAR_JWT_KEY_DIR` pointing at your key directory. Leave
      `AAKAAR_JWT_BOOTSTRAP_KEYS` **unset** in production — it generates
      unencrypted keys for dev only.
- [ ] Set `AAKAAR_JWT_ISSUER` and keep the default `AAKAAR_JWT_AUDIENCE`
      (`aakaar-api`) unless you have multiple verifiers.
- [ ] `AAKAAR_MFA_ENCRYPTION_KEY` — Fernet key so TOTP secrets are encrypted
      at rest. Generate:
      `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`
- [ ] Rotate `AAKAAR_SUPERUSER_EMAIL` / `AAKAAR_SUPERUSER_PASSWORD` after
      first boot; they exist only to bootstrap the first login.

### Secrets at rest

- [ ] `AAKAAR_VAULT_KEY` — Fernet key (comma-separated list during rotation;
      the first key encrypts, the rest still decrypt). Without it, vault
      entries are plaintext JSON and the server logs a startup warning.
- [ ] `AAKAAR_VAULT_REQUIRE_ENCRYPTION=1` — fail closed: refuse to start if
      no vault key is configured. Set this everywhere outside dev.
- [ ] Key rotation procedure: [runbooks/03-vault-key-rotation.md](runbooks/03-vault-key-rotation.md).

### Network posture

- [ ] `AAKAAR_CORS_ORIGINS` — exact origins of your web UI only. The default
      allows the Vite dev server (`localhost:5173`); replace it in production.
- [ ] Bind deliberately: `dev.sh` defaults the API to `0.0.0.0` so LAN agents
      can reach it. Set `AAKAAR_API_HOST=127.0.0.1` (or firewall the port) if
      no remote agents are used.
- [ ] `AAKAAR_OPENAI_TLS_VERIFY` stays `true` unless you run a self-signed
      **local** LLM gateway via `AAKAAR_OPENAI_BASE_URL`. It is only consulted
      for custom base URLs and logs loudly when disabled — never set it to
      `false` against a public endpoint.
- [ ] Keep the rate limiter on (`AAKAAR_RATE_LIMIT_ENABLED=true`, default).
      The `/auth` bucket (`AAKAAR_RATE_LIMIT_AUTH_PER_MIN`, default 20/min)
      blunts credential stuffing.
- [ ] Leave `AAKAAR_ALLOW_LOCAL_PATHS` unset: `file.read_local` (arbitrary
      worker-filesystem reads into the object store) is disabled by default
      and should stay that way on shared hosts.
- [ ] `AAKAAR_REMOTE_EXEC_ENABLED=false` if you do not use remote agents —
      it closes the `/ws/agents` surface entirely.

### Broker (optional agent rendezvous)

If you deploy the connection broker so agents and the API meet at a third
process instead of dialing the API directly:

- [ ] `AAKAAR_BROKER_TOKEN` is **required with no default** — the broker
      process must refuse to start without it. Treat it like a JWT secret.
- [ ] On the API side, setting `AAKAAR_BROKER_URL` makes
      `AAKAAR_BROKER_TOKEN` required there too (fail closed at startup);
      both sides must hold the same token.
- [ ] Bound the blast radius: keep `AAKAAR_BROKER_MAX_SESSIONS` (default 200)
      and `AAKAAR_BROKER_HANDSHAKE_TIMEOUT` (default 10s) at their defaults
      unless you have measured a need.
- [ ] Outage handling and the direct-dial fallback:
      [runbooks/05-broker-outage.md](runbooks/05-broker-outage.md).

### Postgres deployments only

- [ ] Connect as the non-superuser `aakaar_app` role so row-level security
      enforces (see `extras/rls/setup_app_role.sql`), and set
      `AAKAAR_RLS_STRICT=true` once verified.

## Operational security docs

- **Security whitepaper** (trust model, isolation, agent auth, secrets lifecycle,
  RPA attack surface + compromise response): [docs/security-whitepaper.md](docs/security-whitepaper.md)
- **Compliance mapping** (control → endpoint/service/test): [docs/compliance-mapping.md](docs/compliance-mapping.md)
- **Operations manual** (backup/restore, upgrade/rollback, sweeps, health): [docs/operations-manual.md](docs/operations-manual.md)
- **Architecture Decision Records**: [docs/adr/](docs/adr/)
- **Capability authoring guide** (writing a new capability safely): [docs/capability-authoring-guide.md](docs/capability-authoring-guide.md)
- Incident runbooks: [runbooks/](runbooks/)
- Backup/restore of the SQLite primary store:
  [runbooks/01-sqlite-backup-restore.md](runbooks/01-sqlite-backup-restore.md)
- Contribution and review gates: [CONTRIBUTING.md](CONTRIBUTING.md)
