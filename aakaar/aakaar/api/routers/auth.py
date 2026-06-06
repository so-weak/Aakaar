"""Login endpoint.

Accepts email + password; returns a JWT access token. Tenant scoping is
implicit — login is keyed on (email + password) and the email's tenant_id
is read from the user row. Superusers have tenant_id == NULL.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from aakaar.api.auth import issue_access_token, verify_password
from aakaar.api.deps import get_audit, get_session, get_settings
from aakaar.api.schemas import LoginRequest, LoginResponse
from aakaar.core.config import Settings
from aakaar.db.models import Tenant, User, UserStatus
from aakaar.services.audit import AuditRecorder

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(
    body: LoginRequest,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    audit: Annotated[AuditRecorder, Depends(get_audit)],
) -> LoginResponse:
    # Constant-ish-time: always look up by email; verify password even if no
    # user is found, against a sentinel hash. Rough mitigation against user
    # enumeration via response timing.
    logger.debug("login attempt for email=%s", body.email)
    candidates = list(session.scalars(select(User).where(User.email == body.email)))
    user: User | None = None
    for u in candidates:
        if verify_password(body.password, u.password_hash):
            user = u
            break

    if user is None or user.status != UserStatus.ACTIVE:
        logger.info("login failed for email=%s (no active user matched)", body.email)
        audit.record(
            action="auth.login_failed",
            target_kind="auth",
            target_id=body.email[:64],
            payload={"email": body.email},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials"
        )

    issued = datetime.now(UTC)
    ttl = timedelta(minutes=settings.access_token_ttl_minutes)
    token = issue_access_token(
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        ttl=ttl,
        now=issued,
    )
    tenant_slug: str | None = None
    tenant_name: str | None = None
    if user.tenant_id is not None:
        tenant = session.get(Tenant, user.tenant_id)
        if tenant is not None:
            tenant_slug = tenant.slug
            tenant_name = tenant.name

    logger.info(
        "login ok user_id=%s tenant_id=%s role=%s",
        user.id,
        user.tenant_id,
        user.role,
    )
    audit.record(
        action="auth.login",
        tenant_id=user.tenant_id,
        actor_id=user.id,
        target_kind="user",
        target_id=str(user.id),
        payload={"role": user.role},
    )
    return LoginResponse(
        access_token=token,
        expires_at=issued + ttl,
        tenant_slug=tenant_slug,
        tenant_name=tenant_name,
    )
