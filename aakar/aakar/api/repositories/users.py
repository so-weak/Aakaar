"""User repository helpers."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from aakar.api.auth import hash_password
from aakar.db.models import User, UserRole, UserStatus


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


def disable_user(session: Session, user_id: uuid.UUID) -> User | None:
    u = session.get(User, user_id)
    if u is None:
        return None
    u.status = UserStatus.DISABLED
    session.flush()
    return u
