# Aakaar — backend (`aakaar`)

The FastAPI backend: API, planner (LLM → DAG), interpreter/orchestrator,
capability registry, scheduler, audit, remote-execution dispatcher, auth
(password / RS256 / MFA / OIDC), and the database with Alembic migrations.

> New here? Start with the [repo root README](../README.md) for the full-stack
> picture and quickstart. This document is the backend deep-dive.

## Package layout

```
aakaar/aakaar/
├── api/
│   ├── app.py            # FastAPI factory: middleware, routers, lifespan
│   ├── main.py           # production ASGI entrypoint (build_app)
│   ├── deps.py           # AppDependencies bundle + request dependencies + auth chain
│   ├── bootstrap.py      # first-run superuser
│   ├── schemas.py        # request/response models
│   ├── auth/             # jwt, keys (RS256/JWKS), session (mint/verify), totp, oidc, passwords
│   ├── routers/          # auth, mfa, oidc, jwks, workflows, runs, agents, ws, …
│   └── repositories/     # DB access helpers (users, agents, runs, …)
├── capabilities/         # in-tree capabilities, grouped: comms/ data/ files/ integration/ web/ remote/
├── interpreter/          # executor, orchestrator, activities
├── planner/              # NL → DAG planning (LLM + capability index)
├── services/             # scheduler, audit, events
├── core/                 # config, middleware (metrics/rate-limit/request-id), net/ssrf
├── db/                   # models, session (+ RLS GUC bridge), tenancy, migrations/
├── shared/               # registry, dag types
├── storage/ vault/ workers/
```

## Configuration

Config is a `@dataclass` (`core/config.py`) built from `AAKAAR_*` env vars by
`load_settings()`. Tests construct `Settings(...)` directly. The only required
var is `AAKAAR_JWT_SECRET`. See the [env table in the root README](../README.md#key-environment-variables)
and `core/config.py` for the full set (DB, LLM, scheduler, remote-exec, auth).

## Running

```bash
cd aakaar
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]' -e ../aakaar-capabilities
export AAKAAR_JWT_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
alembic upgrade head
uvicorn aakaar.api.main:app --reload --host 0.0.0.0 --port 8000
```

OpenAPI docs at `http://localhost:8000/docs`; health at `/healthz`; Prometheus
metrics at `/metrics`.

## Database & migrations

Two dialects, chosen by `AAKAAR_DB_URL`: **SQLite** (default, dev) and
**`postgresql+psycopg`** (Postgres/Yugabyte, prod — `pip install -e '.[postgres]'`
for the driver; the Docker image bundles it). Migrations are linear Alembic
revisions in `db/migrations/versions/`:

| Rev | Adds |
|-----|------|
| `0001`–`0005` | core schema, remote agents, schedules. |
| `0006` | MFA/OIDC user columns (`totp_secret`, `mfa_recovery_codes`, `oidc_subject` unique, …). |
| `0007` | **Row-Level Security** policies (Postgres only; no-op on SQLite). |

```bash
alembic upgrade head        # apply
alembic downgrade -1        # roll back one
```

## Authentication subsystem

```mermaid
flowchart TD
    subgraph Mint
      LOGIN[/auth/login/] --> SESS[auth/session.mint_access_token]
      MFAV[/auth/mfa/verify/] --> SESS
      OIDC[/auth/oidc/callback/] --> SESS
      SESS -->|HS256| SECRET[jwt_secret]
      SESS -->|RS256| KS[KeyStore.active kid]
    end
    subgraph Verify
      REQ[every request] --> GCC[deps.get_current_claims]
      GCC --> VAT[auth/session.verify_access_token]
      VAT -->|alg pinned| OK[TokenClaims incl. amr]
      OK --> GCU[get_current_user: status, role-drift,<br/>tenant-suspend, MFA-amr enforcement]
    end
    KS --> JWKS[/auth/.well-known/jwks.json/]
```

- **`auth/jwt.py`** — `issue_access_token` / `verify_token`. Verification pins the
  algorithm and never trusts the token’s `alg`; access tokens carrying the MFA
  ticket audience are rejected.
- **`auth/keys.py`** — `KeyStore` loads `<kid>.pem` from `AAKAAR_JWT_KEY_DIR`,
  picks the active kid, exposes a `kid → public_pem` resolver, and emits JWKS.
  Set `AAKAAR_JWT_BOOTSTRAP_KEYS=true` (dev) to generate a keypair.
- **`auth/session.py`** — the one seam every login path funnels through (HS vs RS
  selection, issuer/audience policy, the MFA ticket + binding hash).
- **`auth/totp.py`** — TOTP with time-step anti-replay, single-use bcrypt-hashed
  recovery codes, and optional Fernet encryption-at-rest.
- **`auth/oidc.py`** — confidential OIDC client (discovery + PKCE + nonce +
  id_token verification + `userinfo.sub` check + email_verified gate).
- **Enforcement** (`deps.py`): `get_current_user` rejects a user with MFA enabled
  whose token lacks a second-factor `amr`; `require_mfa_satisfied` is a step-up
  guard for sensitive routes.

### Enabling RS256

```bash
export AAKAAR_JWT_ALG=RS256
export AAKAAR_JWT_KEY_DIR=/secure/aakaar/keys
export AAKAAR_JWT_BOOTSTRAP_KEYS=true      # dev only; provision real keys in prod
# rotate: drop a newer <kid>.pem in the dir + update `active`; old tokens keep
# validating because every public key is published at the JWKS endpoint.
```

### Enabling OIDC / MFA

OIDC: set `AAKAAR_OIDC_ENABLED=true` + `AAKAAR_OIDC_ISSUER/_CLIENT_ID/_CLIENT_SECRET/_REDIRECT_URI`
(register `…/auth/oidc/callback` with your IdP). MFA needs no server config to
work; set `AAKAAR_MFA_ENCRYPTION_KEY` (a Fernet key) to encrypt secrets at rest
and `AAKAAR_MFA_ISSUER` for the authenticator label.

## Row-Level Security (Postgres)

`db/tenancy.py` holds the active scope in a contextvar; `db/session.py` mirrors it
into the transaction-local `app.tenant_id` GUC before each statement (Postgres
only); migration `0007` adds `FORCE ROW LEVEL SECURITY` policies with `WITH CHECK`
on every tenant table, plus the `tenants` (id-keyed) and `chat_messages`
(join-scoped) tables. The marker is a tenant UUID, `"system"` (trusted
cross-tenant), or `""` (deny — only under `AAKAAR_RLS_STRICT=true`).

**RLS enforces whenever the app connects as a non-superuser role that owns its
tables** — Postgres applies `FORCE ROW LEVEL SECURITY` to a table’s owner, and
superuser/`BYPASSRLS` roles always bypass. So the whole rollout is: *don’t
connect as the postgres superuser.* No privilege separation, no second
connection — one `aakaar_app` role runs the migrations (owning what it creates)
and serves traffic.

- **docker-compose:** automatic. The init hook
  ([`extras/rls/01-create-app-role.sh`](../extras/rls/01-create-app-role.sh))
  creates `aakaar_app` on a fresh volume and the API connects as it — RLS is on
  out of the box.
- **Manual / existing DB:** once, as an admin —
  ```bash
  psql "$ADMIN_DB_URL" -v app_password="'strong-pass'" -f ../extras/rls/setup_app_role.sql
  export AAKAAR_DB_URL="postgresql+psycopg://aakaar_app:strong-pass@host/aakaar"
  ```

Verified on real Postgres: a tenant sees only its rows, `system` sees all, an
empty marker denies all, and `WITH CHECK` blocks cross-tenant writes.

> **Strict mode** (`AAKAAR_RLS_STRICT=true`) makes a no-scope session fail-closed
> (deny-all) instead of falling back to `system`. The always-on paths (login,
> scheduler, run execution, bootstrap) already enter `system_scope`/`tenant_scope`;
> before enabling strict, wrap any custom superuser/stats endpoint that queries
> across tenants in `system_scope()` so it isn’t denied.

## Capabilities & MCP

Build the full registry exactly as the app does:

```python
from aakaar.shared.registry import build_default_registry
from aakaar.interpreter.activities.registry import ActivityRegistry
from aakaar.capabilities import load_into
reg = build_default_registry(); load_into(reg, ActivityRegistry())
```

[`aakaar-mcp`](../aakaar-mcp/) projects this same registry as MCP tools.

## Testing & quality

```bash
.venv/bin/python -m pytest          # tests/  (asyncio_mode=auto, warnings-as-errors)
.venv/bin/python -m ruff check aakaar tests
.venv/bin/python -m mypy aakaar      # strict
```
