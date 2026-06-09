"""Tenancy guard.

Every request handler must enter a `tenant_scope(tenant_id)` block before
touching domain tables. Repository functions consult `current_tenant()` and
refuse to run without one set.

This is the application-layer half of multi-tenancy. On Yugabyte/Postgres it
is mirrored to a Postgres GUC (`app.tenant_id`) that Row-Level Security
policies read — see `db/session.py` (the `set_config` listener) and the
`*_row_level_security` migration. The marker is one of:

  * a tenant UUID  — inside `tenant_scope(tid)`; RLS restricts to that tenant.
  * ``"system"``   — inside `system_scope()`; trusted cross-tenant access
                     (login lookups, superuser/stats, schedulers, bootstrap).
  * ``""``         — no scope active AND `rls_strict` is on; RLS denies all
                     rows (fail-closed).

SQLite has no RLS — the contextvar is the only line of defense, which is fine
for dev. The GUC also only *enforces* on Postgres when the app connects as a
non-superuser, non-owner role (table owners and superusers bypass RLS).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

SYSTEM_MARKER = "system"


class TenancyError(RuntimeError):
    """Raised when tenant-scoped code runs without a tenant set, or when a
    nested scope tries to switch tenants."""


_TENANT: ContextVar[uuid.UUID | None] = ContextVar("aakaar_tenant", default=None)
_SYSTEM: ContextVar[bool] = ContextVar("aakaar_system", default=False)


def current_tenant() -> uuid.UUID:
    """Return the current tenant id, or raise if no scope is active."""
    tid = _TENANT.get()
    if tid is None:
        raise TenancyError("no tenant scope is active; wrap the call in tenant_scope(...)")
    return tid


def current_tenant_or_none() -> uuid.UUID | None:
    """Return the current tenant id without raising. Useful for superuser
    paths where tenant context is intentionally absent."""
    return _TENANT.get()


@contextmanager
def tenant_scope(tenant_id: uuid.UUID) -> Iterator[None]:
    """Bind a tenant id for the duration of a block.

    Nested scopes for the *same* tenant are allowed (idempotent). Switching
    tenants inside an active scope raises — that almost always indicates a
    leaked request context.
    """
    if not isinstance(tenant_id, uuid.UUID):
        raise TenancyError(f"tenant_id must be a UUID, got {type(tenant_id).__name__}")
    existing = _TENANT.get()
    if existing is not None and existing != tenant_id:
        raise TenancyError(
            f"cannot enter tenant_scope({tenant_id}) inside existing scope for {existing}"
        )
    token = _TENANT.set(tenant_id)
    try:
        yield
    finally:
        _TENANT.reset(token)


@contextmanager
def system_scope() -> Iterator[None]:
    """Mark a block as trusted, cross-tenant system context.

    Use this for the handful of paths that legitimately span tenants before a
    tenant is known: the login lookup, superuser/stats queries, the scheduler
    poll, audit writes, and superuser bootstrap. Under RLS the GUC is set to
    the ``"system"`` marker, which the policies treat as "see/modify all rows".
    Making system access *explicit* is what lets `rls_strict` deny the
    accidental no-scope case without breaking these flows.
    """
    token = _SYSTEM.set(True)
    try:
        yield
    finally:
        _SYSTEM.reset(token)


def in_system_scope() -> bool:
    return _SYSTEM.get()


def rls_marker(*, strict: bool) -> str:
    """Resolve the value for the `app.tenant_id` GUC from the active scope.

    A tenant scope wins (most specific); then an explicit system scope; then,
    with nothing set, ``"system"`` (allow-all, backward-compatible) unless
    ``strict`` is on, in which case ``""`` (deny-all, fail-closed).
    """
    tid = _TENANT.get()
    if tid is not None:
        return str(tid)
    if _SYSTEM.get():
        return SYSTEM_MARKER
    return "" if strict else SYSTEM_MARKER
