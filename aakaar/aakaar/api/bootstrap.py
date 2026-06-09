"""Startup helpers.

`bootstrap_superuser` ensures at least one superuser exists when the app
starts. It reads credentials from settings; if either is missing or a
superuser already exists, it's a no-op.

This is the only path that creates a superuser — there is no API for it.
Rotating the credential is done by setting new env vars and restarting,
which creates a fresh superuser row alongside the old one (which the
superuser can then disable through `/superuser/...` endpoints, once we
add a disable-self-or-peer route in P2).
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from aakaar.api.repositories import users as users_repo
from aakaar.core.config import Settings
from aakaar.db.models import User, UserRole
from aakaar.db.session import SessionFactory
from aakaar.db.tenancy import system_scope

logger = logging.getLogger(__name__)


def bootstrap_superuser(settings: Settings, session_factory: SessionFactory) -> None:
    if not settings.superuser_email or not settings.superuser_password:
        logger.info("superuser bootstrap: skipped (env vars not set)")
        return
    # Superuser rows have tenant_id = NULL and the lookup spans all tenants —
    # trusted cross-tenant work, so run it under a system scope (the RLS marker
    # that may read/write outside any single tenant).
    with system_scope(), session_factory.session() as s:
        existing = s.scalars(
            select(User).where(User.role == UserRole.SUPERUSER)
        ).first()
        if existing is not None:
            logger.info("superuser bootstrap: superuser already exists")
            return
        users_repo.create_user(
            s,
            tenant_id=None,
            email=settings.superuser_email,
            password=settings.superuser_password,
            role=UserRole.SUPERUSER,
        )
        s.commit()
        logger.info("superuser bootstrap: created superuser %s", settings.superuser_email)
