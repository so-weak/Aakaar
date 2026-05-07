"""Tenancy guard.

Every request handler must enter a `tenant_scope(tenant_id)` block before
touching domain tables. Repository functions consult `current_tenant()` and
refuse to run without one set.

This is the application-layer half of multi-tenancy. On Yugabyte/Postgres we
will additionally turn on Row-Level Security in a follow-up migration; the
contextvar will then be mirrored to a Postgres GUC (`app.tenant_id`) so RLS
policies can use it. SQLite has no RLS — the contextvar is the only line of
defense, which is fine for dev.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar


class TenancyError(RuntimeError):
    """Raised when tenant-scoped code runs without a tenant set, or when a
    nested scope tries to switch tenants."""


_TENANT: ContextVar[uuid.UUID | None] = ContextVar("aakar_tenant", default=None)


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
