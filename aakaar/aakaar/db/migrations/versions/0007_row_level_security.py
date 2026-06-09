"""Postgres Row-Level Security (tenant isolation, defense-in-depth)

Adds an RLS policy to every tenant-scoped table keyed on the transaction-local
`app.tenant_id` GUC that `db/session.py` mirrors from the active tenancy scope.

Design notes (deliberately stricter than a naive port):
  * Marker semantics — ``"system"`` = trusted cross-tenant (login, superuser,
    schedulers); a UUID = that tenant only; ``""`` or NULL (unset) = DENY ALL.
    This is fail-closed: a forgotten scope under rls_strict sees nothing rather
    than everything.
  * ``FORCE ROW LEVEL SECURITY`` — without it, the *table owner* bypasses RLS.
    With it, even the owner is constrained (Postgres SUPERUSER / BYPASSRLS
    roles still bypass — so the app must connect as a dedicated non-superuser,
    non-owner role; see extras/rls/setup_app_role.sql).
  * ``WITH CHECK`` — pins INSERT/UPDATE to the active tenant so a tenant
    session cannot forge rows for another tenant (or NULL/system rows).
  * Idempotent — DROP POLICY IF EXISTS before CREATE so re-runs don't error.

SQLite has no RLS; the whole migration is a no-op there (the contextvar remains
the only isolation layer in dev).

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-10
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables carrying a `tenant_id` column. `users` and `audit_log` have a NULLABLE
# tenant_id (superuser / system rows): the tenant branch `tenant_id = <uuid>`
# naturally excludes NULL rows from a tenant's view, while the system branch
# sees them.
_TENANT_COL_TABLES = (
    "users",
    "capability_grants",
    "workflows",
    "workflow_versions",
    "runs",
    "run_events",
    "chat_sessions",
    "audit_log",
    "workflow_schedules",
    "remote_agents",
)

# Predicate builder. A CASE — not OR — because Postgres does NOT guarantee OR
# short-circuits, so a plain `… = 'system' OR col = current_setting(…)::uuid`
# still evaluates the ::uuid cast when the marker is 'system'/'' and raises
# `invalid input syntax for type uuid`. CASE only evaluates the matching branch,
# so the cast runs *only* for a real-UUID marker.
_SYSTEM = "current_setting('app.tenant_id', true) = 'system'"


def _match(expr: str) -> str:
    """`'system'` → all rows; `''`/unset → none; else `expr` must equal the GUC uuid."""
    return (
        "CASE current_setting('app.tenant_id', true) "
        "WHEN 'system' THEN true "
        "WHEN '' THEN false "
        f"ELSE {expr} = current_setting('app.tenant_id', true)::uuid END"
    )


def _enable(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def _policy(table: str, using: str, check: str) -> None:
    name = f"{table}_tenant_isolation"
    op.execute(f"DROP POLICY IF EXISTS {name} ON {table}")
    op.execute(
        f"CREATE POLICY {name} ON {table} "
        f"USING ({using}) WITH CHECK ({check})"
    )


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    for table in _TENANT_COL_TABLES:
        _enable(table)
        pred = _match("tenant_id")
        _policy(table, using=pred, check=pred)

    # tenants: no tenant_id column — a tenant may only see its own row, and only
    # the system context may create/modify tenant rows.
    _enable("tenants")
    _policy("tenants", using=_match("id"), check=_SYSTEM)

    # chat_messages: no tenant_id — scoped through its parent chat_sessions.
    _enable("chat_messages")
    join_pred = (
        "CASE current_setting('app.tenant_id', true) "
        "WHEN 'system' THEN true "
        "WHEN '' THEN false "
        "ELSE EXISTS (SELECT 1 FROM chat_sessions s "
        "WHERE s.id = chat_messages.session_id "
        "AND s.tenant_id = current_setting('app.tenant_id', true)::uuid) END"
    )
    _policy("chat_messages", using=join_pred, check=join_pred)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in (*_TENANT_COL_TABLES, "tenants", "chat_messages"):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
