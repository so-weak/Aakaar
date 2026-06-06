"""User repository helpers."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from aakaar.api.auth import hash_password
from aakaar.db.models import User, UserRole, UserStatus


class EmailTaken(ValueError):
    pass


def create_user(
    session: Session,
    *,
    tenant_id: uuid.UUID | None,
    email: str,
    password: str,
    role: str,
) -> User:
    if role not in (UserRole.SUPERUSER, UserRole.TENANT_ADMIN, UserRole.TENANT_USER):
        raise ValueError(f"invalid role: {role!r}")
    stmt = select(User).where(User.email == email)
    if tenant_id is None:
        stmt = stmt.where(User.tenant_id.is_(None))
    else:
        stmt = stmt.where(User.tenant_id == tenant_id)
    if session.scalars(stmt).first() is not None:
        raise EmailTaken(email)

    u = User(
        tenant_id=tenant_id,
        email=email,
        password_hash=hash_password(password),
        role=role,
        status=UserStatus.ACTIVE,
    )
    session.add(u)
    session.flush()
    return u


def list_users_for_tenant(session: Session, tenant_id: uuid.UUID) -> list[User]:
    return list(
        session.scalars(
            select(User).where(User.tenant_id == tenant_id).order_by(User.created_at)
        )
    )


def list_all_users(session: Session) -> list[User]:
    """Return every user across every tenant. Superuser-only — tenant-scoped
    callers must use list_users_for_tenant."""
    return list(session.scalars(select(User).order_by(User.created_at)))


def get_user(session: Session, user_id: uuid.UUID) -> User | None:
    return session.get(User, user_id)


def update_user(
    session: Session,
    user_id: uuid.UUID,
    *,
    role: str | None = None,
    password: str | None = None,
) -> User | None:
    """Update a user's role and/or password. Returns the updated row, or
    None if the id doesn't exist. Caller is responsible for tenant + role
    authorization checks."""
    u = session.get(User, user_id)
    if u is None:
        return None
    if role is not None:
        if role not in (UserRole.TENANT_ADMIN, UserRole.TENANT_USER):
            # Mutating to/from SUPERUSER is not a tenant-admin operation.
            raise ValueError(f"role {role!r} is not assignable from this surface")
        u.role = role
    if password is not None:
        u.password_hash = hash_password(password)
    session.flush()
    return u


def set_user_status(
    session: Session, user_id: uuid.UUID, *, status: str
) -> User | None:
    """Flip a user's lifecycle status (active ↔ disabled). Returns the
    updated row, or None if the id doesn't exist."""
    if status not in (UserStatus.ACTIVE, UserStatus.DISABLED):
        raise ValueError(f"invalid status: {status!r}")
    u = session.get(User, user_id)
    if u is None:
        return None
    u.status = status
    session.flush()
    return u


def disable_user(session: Session, user_id: uuid.UUID) -> User | None:
    """Back-compat alias for `set_user_status(..., status='disabled')`."""
    return set_user_status(session, user_id, status=UserStatus.DISABLED)


def disable_users_for_tenant(session: Session, tenant_id: uuid.UUID) -> int:
    """Mark every active user in `tenant_id` as DISABLED. Returns the
    count flipped. Used by the tenant-suspend cascade so a suspended
    tenant's users can't authenticate against the API."""
    from sqlalchemy import update

    result = session.execute(
        update(User)
        .where(
            User.tenant_id == tenant_id,
            User.status == UserStatus.ACTIVE,
        )
        .values(status=UserStatus.DISABLED)
    )
    session.flush()
    return int(result.rowcount or 0)
