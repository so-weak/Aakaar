# ADR 0003: Application-level tenant scoping, optional Postgres RLS

- **Status:** Accepted
- **Date:** 2026-06-15
- **Deciders:** Platform engineering, Security

## Context

Aakaar is multi-tenant: one deployment may serve several legal entities, and a
cross-tenant data leak is a reportable incident. The primary store is SQLite
(ADR 0001), which has **no row-level security**. We need a tenant-isolation
model that is the same code on SQLite and Postgres, with an extra hardening
layer available where the database can enforce it.

## Decision

**Primary guard: application-level scoping.** Every tenant-owned row carries a
`tenant_id`. The session layer (`aakaar/aakaar/db/tenancy.py`) scopes queries to
the caller's tenant, and routers resolve resources only within that tenant — a
cross-tenant id reads as **404** (opaque "not found", so existence cannot be
probed). System actors (login, scheduler, superuser) run under an explicit
`system_scope`.

**Defense-in-depth on Postgres: Row-Level Security.** When `AAKAAR_DB_URL`
points at Postgres and the app connects as a **non-superuser, table-owning**
role (`aakaar_app`, see `extras/rls/setup_app_role.sql`), the tenancy scope is
mirrored into a transaction-local `app.tenant_id` GUC that RLS policies read.
`AAKAAR_RLS_STRICT=true` makes the no-scope case deny-all (fail closed) instead
of mapping to the system marker. On SQLite, RLS is a no-op and the application
guard stands alone.

## Consequences

**Positive**

- One isolation codebase across both backends; tests exercise the same scoping
  logic they ship with.
- Postgres deployments get a second, database-enforced layer that holds even if
  an application query forgets to scope.
- Opaque 404s prevent cross-tenant existence probing.

**Negative / accepted trade-offs**

- **On SQLite, isolation is only as strong as the application code.** A query
  that bypasses the scoping helper would bypass isolation. This is mitigated by
  routing all tenant reads/writes through the scoped session and by tenant
  isolation being explicitly in scope for the security policy
  ([SECURITY.md](../../SECURITY.md)).
- **Filesystem access defeats it.** Anyone who can read `aakaar.sqlite` reads
  every tenant's metadata regardless of app logic, so host hardening and file
  permissions are part of the trust boundary (see ADR 0001 and the security
  whitepaper).
- RLS only actually enforces under the correct Postgres role; a misconfigured
  connection (superuser, or the table owner) silently bypasses it — hence the
  `aakaar_app` role and the documented setup.

## Alternatives considered

- **Database-per-tenant.** Rejected: multiplies operational surface (backup,
  migration, vault layout) against the no-infra and small-ops-team constraints.
- **Rely on RLS alone.** Rejected: SQLite has none, and we will not ship a model
  that only works on one backend.
