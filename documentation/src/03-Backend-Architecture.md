# Aakar — Backend Architecture (v1)

> Two FastAPI services, one shared philosophy: every row is tenant-scoped, every capability is registered, every run is auditable. This document covers the request lifecycle, the database, the run lifecycle, and the operational concerns.

---

## 1. Two backends

| Service | Path | Port (dev) | Owners | Purpose |
| --- | --- | --- | --- | --- |
| `aakar API` | `aakar/aakar/api` | 8000 | Tenant traffic | Auth, chat, runs, vault, objects, admin (per-tenant). |
| `admin-app API` | `admin-app/server` | 8001 | Platform traffic | Brahma's surfaces: tenant CRUD, recon upload, capability publishing. |

Both speak FastAPI plus Pydantic v2 plus SQLAlchemy 2. They share migration tooling (Alembic) but maintain separate schemas in v1. The split is intentional: platform admin traffic must not contend with tenant traffic, and platform admin endpoints have a wider blast radius.

## 2. Stack

| Concern | v1 choice |
| --- | --- |
| Language | Python 3.12 |
| Web framework | FastAPI |
| Validation | Pydantic v2 |
| ORM | SQLAlchemy 2 |
| Migrations | Alembic |
| Local DB | SQLite |
| Cloud DB | Yugabyte (Postgres wire) |
| Vector index | FAISS (SQLite mode), pgvector (Yugabyte mode) |
| Object store | Filesystem under `AAKAR_DATA_DIR` |
| Browser worker | Playwright headless Chromium |
| HTTP worker | httpx |
| Auth | bcrypt + HS256 JWT |
| LLM | OpenAI Chat Completions strict JSON |
| Embeddings | BGE small via sentence-transformers |
| Background work | asyncio + a small threadpool |

## 3. App factory

```mermaid
flowchart TD
  S["uvicorn boots aakar.api.main:app"] --> F["create_app"]
  F --> C["load settings from env"]
  C --> D["create engine and session factory"]
  D --> R["mount routers (auth, admin, chat, runs, vault, objects)"]
  R --> M["install middleware (cors, request id, audit)"]
  M --> H["healthz, readyz"]
  H --> RDY["app ready"]
```

The factory pattern keeps tests cheap: each integration test builds a fresh app bound to a temp SQLite file.

## 4. Database schema

```mermaid
erDiagram
  TENANTS ||--o{ USERS : has
  TENANTS ||--o{ CAPABILITY_GRANTS : grants
  TENANTS ||--o{ WORKFLOWS : owns
  WORKFLOWS ||--o{ WORKFLOW_VERSIONS : versions
  WORKFLOW_VERSIONS ||--o{ RUNS : produces
  RUNS ||--o{ RUN_EVENTS : emits
  USERS ||--o{ CHAT_SESSIONS : owns
  CHAT_SESSIONS ||--o{ CHAT_MESSAGES : contains
  TENANTS ||--o{ AUDIT_LOG : writes
  USERS ||--o{ AUDIT_LOG : writes

  TENANTS {
    string id PK
    string slug
    string display_name
    datetime created_at
  }
  USERS {
    string id PK
    string tenant_id FK
    string email
    string password_hash
    string role
    datetime created_at
  }
  CAPABILITY_GRANTS {
    string id PK
    string tenant_id FK
    string capability_id
    json scopes
  }
  WORKFLOWS {
    string id PK
    string tenant_id FK
    string name
  }
  WORKFLOW_VERSIONS {
    string id PK
    string workflow_id FK
    int version
    json dag
    datetime created_at
  }
  RUNS {
    string id PK
    string tenant_id FK
    string user_id FK
    string workflow_version_id FK
    string status
    datetime started_at
    datetime ended_at
  }
  RUN_EVENTS {
    string id PK
    string run_id FK
    string kind
    json payload
    datetime emitted_at
  }
  CHAT_SESSIONS {
    string id PK
    string user_id FK
    string title
    datetime created_at
  }
  CHAT_MESSAGES {
    string id PK
    string session_id FK
    string role
    text content
    datetime created_at
  }
  AUDIT_LOG {
    string id PK
    string tenant_id FK
    string user_id FK
    string action
    json detail
    datetime occurred_at
  }
```

Conventions:

- All ids are ULIDs encoded as 26-char strings.
- Every tenant-bearing table has a unique index on `(tenant_id, ...)` first; Postgres planner uses it.
- `RUN_EVENTS.payload` is JSON; PII fields are scrubbed before write.
- `AUDIT_LOG` is append-only; deletes are forbidden by an Alembic check.

## 5. Request lifecycle

```mermaid
sequenceDiagram
  autonumber
  participant C as Client
  participant MW as Middleware
  participant R as Router
  participant D as Deps
  participant Q as Repository
  participant DB as Database

  C->>MW: HTTP request with JWT
  MW->>MW: assign request_id
  MW->>R: dispatch
  R->>D: get_db
  D-->>R: session
  R->>D: require_user
  D->>DB: load user
  DB-->>D: row
  D-->>R: User
  R->>Q: domain call(user.tenant_id, ...)
  Q->>DB: query scoped by tenant
  DB-->>Q: rows
  Q-->>R: typed result
  R-->>MW: response model
  MW->>MW: audit log write (if mutating)
  MW-->>C: JSON response
```

Tenant scoping is enforced inside repositories. Routers cannot accidentally widen the scope because repositories require a `tenant_id` argument and ignore any free-form filter that would broaden it.

## 6. Run lifecycle

```mermaid
stateDiagram-v2
  [*] --> queued
  queued --> running: dispatcher
  running --> waiting_for_signal: capability publishes signal
  waiting_for_signal --> running: SignalHub.resolve
  running --> succeeded: terminal node ok
  running --> failed: any node throws
  running --> cancelled: user cancels
  succeeded --> [*]
  failed --> [*]
  cancelled --> [*]
```

A retry of a failed run creates a new `RUNS` row with a `parent_run_id` reference. Audit history is preserved.

## 7. Vault

The vault stores per-tenant credentials. Each entry binds:

- `tenant_id`
- `site_id` (the third party, for example `nbbl` or `hdfc`)
- `user_handle` (the operator's identity on that site)
- `secret_blob` (encrypted symmetric ciphertext)
- `metadata` (rotation hints, last-used)

```mermaid
flowchart LR
  OP["Operator"] -->|"set or rotate"| API["Vault API"]
  API --> ENC["Symmetric encrypt with tenant key"]
  ENC --> DB[("vault_entries")]
  CAP["Capability at run time"] --> API
  API --> DEC["Decrypt with tenant key"]
  DEC --> CAP
```

In v1 the symmetric key is read from environment; production should source it from a managed KMS. The vault adapter is interface-driven so this swap touches one file.

## 8. Object storage

Artifacts (downloaded files, screenshots, run-time uploads) are persisted under:

```
$AAKAR_DATA_DIR/
  tenants/
    <tenant_id>/
      runs/
        <run_id>/
          events/
            <event_id>.png
          downloads/
            <filename>
          uploads/
            <filename>
```

Each row that references an artifact stores its URI as `file://...` in v1. Switching to S3 or GCS is one adapter implementation away; row-level URIs already encode the namespace.

## 9. FAISS index

The capability search index is built at process start from the registered capability set. It supports:

- top-k similarity search by prompt embedding
- metadata filter by tenant grants
- rebuild on capability registration

The index is a single FAISS `IndexFlatIP` plus a parallel sidecar list of capability ids. For Yugabyte deployments the same data lives in a `pgvector` table and the planner uses a SQL ANN query instead. Behavior is identical from the planner's perspective.

## 10. Stats and dashboard queries

The frontend dashboard pulls aggregates through three endpoints:

| Endpoint | Returns |
| --- | --- |
| `/api/stats/runs/daily` | run counts per day, last 30 days, by status |
| `/api/stats/capabilities/usage` | top capabilities by run count |
| `/api/stats/sites/health` | per-site success rate over last 7 days |

All three are materialized at query time from `RUNS` and `RUN_EVENTS` with simple `GROUP BY` aggregations. v1 makes no attempt at pre-aggregation.

## 11. Settings table

A small `settings` table holds per-tenant feature flags and tunables:

| Key | Default | Notes |
| --- | --- | --- |
| `planner.strategy` | `auto` | `auto`, `oneshot`, or `agentic`. |
| `executor.max_concurrent_runs` | `4` | Per-tenant cap on parallel runs. |
| `browser.timeout_ms` | `30000` | Default per-step timeout. |
| `vault.rotation_days` | `90` | Reminder cadence; non-blocking. |

## 12. Bootstrap and seeding

On first boot, the API:

1. Runs Alembic migrations to head.
2. Seeds platform metadata: capability registry rows from code, control rows from YAML.
3. Optionally seeds a demo tenant with an admin user if `AAKAR_SEED_DEMO=1`.
4. Builds the FAISS index in memory from the seeded capabilities.

The seed step is idempotent. Re-running it does not duplicate rows.

## 13. admin-app server

The admin-app's FastAPI service is intentionally thin:

- It owns the recon upload endpoint (`/api/recon/uploads`) and the upload history list.
- It does not access the tenant database. Brahma surfaces that need cross-tenant data go through the aakar API with a `brahma` JWT.
- Uploaded recon files are stored on disk under `admin-app/server/uploads/` and referenced from the in-memory history table for v1.

## 14. Operations runbook (v1, dev)

| Task | Command |
| --- | --- |
| Start everything | `./start.sh` |
| Run migrations | `(cd aakar && .venv/bin/alembic upgrade head)` |
| Run backend tests | `(cd aakar && .venv/bin/pytest)` |
| Reset local DB | `rm aakar/data/aakar.db && alembic upgrade head` |
| Tail aakar logs | follow stdout in the API Terminal window |
| Stop a service | Ctrl-C in its window or `kill.sh` |

`start.sh` opens five Terminal windows: aakar API (8000), admin-app API (8001), aakar-web (5173), admin-app (3000), nbbl-app (3001). Numerical libs are pinned to single-thread mode in the env to avoid leaked-semaphore warnings on shutdown.

## 15. Failure modes

| Scenario | Detection | Behavior |
| --- | --- | --- |
| LLM returns malformed JSON | Pydantic parse | Retry once then surface error to chat. |
| LLM emits unknown capability | DAG validator | Reject DAG, return clarification. |
| Browser crashes mid-run | Heartbeat timeout | Mark run failed; preserve events; surface retry button. |
| Vault entry missing | Capability lookup | Pause with a `picker` signal asking the operator to add credentials. |
| Disk full | Object write error | Mark run failed; alert via audit log. |
| Migration failure | Alembic | Refuse to start; print the failing revision. |

## 16. Backups (v1, manual)

Local SQLite is in `aakar/data/aakar.db`; an `sqlite3 .backup` run from cron is sufficient for dev. Yugabyte is backed up via the platform's standard snapshot tooling. Object store contents (artifacts) are tarred per run when a run finishes; the tarball is sufficient evidence to replay UI state.

## 17. Reading guide

- For motivation and boundaries, read the HLD.
- For per-module details, read the LLD.
- For UI shape and live screen, read the frontend doc.
- For what comes next (including observability), read the roadmap.
