"""Login endpoint.

Accepts email + password; returns a JWT access token — or, when the user has
MFA enabled, a short-lived step-up ticket (`mfa_required=true`) to be completed
at /auth/mfa/verify. Tenant scoping is implicit: login is keyed on
(email + password) and the email's tenant_id is read from the user row.
Superusers have tenant_id == NULL.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from aakaar.api.auth import (
    KeyStore,
    mfa_binding_hash,
    mint_access_token,
    mint_mfa_ticket,
    verify_password,
)
from aakaar.api.deps import get_audit, get_key_store, get_session, get_settings
from aakaar.api.schemas import LoginRequest, LoginResponse
from aakaar.core.config import Settings
from aakaar.db.models import Tenant, User, UserStatus
from aakaar.db.tenancy import system_scope
from aakaar.services.audit import AuditRecorder

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(
    body: LoginRequest,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    key_store: Annotated[KeyStore | None, Depends(get_key_store)],
    audit: Annotated[AuditRecorder, Depends(get_audit)],
) -> LoginResponse:
    # The lookup is cross-tenant (we don't know the tenant until we find the
    # user), so it runs in a system scope — under RLS that is the trusted
    # marker that may read across tenants.
    logger.debug("login attempt for email=%s", body.email)
    with system_scope():
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

    tenant_slug: str | None = None
    tenant_name: str | None = None
    if user.tenant_id is not None:
        with system_scope():
            tenant = session.get(Tenant, user.tenant_id)
        if tenant is not None:
            tenant_slug = tenant.slug
            tenant_name = tenant.name

    # Password is correct. If MFA is enabled, stop here and hand back a
    # short-lived ticket bound to the user's current security state.
    if user.mfa_enabled:
        ticket = mint_mfa_ticket(
            settings,
            key_store,
            user_id=user.id,
            tenant_id=user.tenant_id,
            role=user.role,
            bnd=mfa_binding_hash(
                password_hash=user.password_hash,
                status=user.status,
                totp_secret=user.totp_secret,
            ),
            now=issued,
        )
        logger.info("login: password ok, MFA required user_id=%s", user.id)
        audit.record(
            action="auth.mfa_challenge",
            tenant_id=user.tenant_id,
            actor_id=user.id,
            target_kind="user",
            target_id=str(user.id),
        )
        return LoginResponse(
            mfa_required=True,
            mfa_token=ticket,
            tenant_slug=tenant_slug,
            tenant_name=tenant_name,
        )

    ttl = timedelta(minutes=settings.access_token_ttl_minutes)
    token = mint_access_token(
        settings,
        key_store,
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
        amr=("pwd",),
        ttl=ttl,
        now=issued,
    )
    user.last_login_at = issued
    session.commit()

    logger.info(
        "login ok user_id=%s tenant_id=%s role=%s", user.id, user.tenant_id, user.role
    )
    audit.record(
        action="auth.login",
        tenant_id=user.tenant_id,
        actor_id=user.id,
        target_kind="user",
        target_id=str(user.id),
        payload={"role": user.role, "amr": "pwd"},
    )
    return LoginResponse(
        access_token=token,
        expires_at=issued + ttl,
        tenant_slug=tenant_slug,
        tenant_name=tenant_name,
    )
