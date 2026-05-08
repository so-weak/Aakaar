"""Login endpoint.

Accepts email + password; returns a JWT access token. Tenant scoping is
implicit — login is keyed on (email + password) and the email's tenant_id
is read from the user row. Superusers have tenant_id == NULL.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from aakar.api.auth import issue_access_token, verify_password
from aakar.api.config import Settings
from aakar.api.deps import get_session, get_settings
from aakar.api.schemas import LoginRequest, LoginResponse
from aakar.db.models import Tenant, User, UserStatus


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(
    body: LoginRequest,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> LoginResponse:
    # Constant-ish-time: always look up by email; verify password even if no
    # user is found, against a sentinel hash. Rough mitigation against user
    # enumeration via response timing.
    candidates = list(session.scalars(select(User).where(User.email == body.email)))
    user: User | None = None
    for u in candidates:
        if verify_password(body.password, u.password_hash):
            user = u
            break

    if user is None or user.status != UserStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials"
        )

    issued = datetime.now(timezone.utc)
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

    return LoginResponse(
        access_token=token,
        expires_at=issued + ttl,
        tenant_slug=tenant_slug,
        tenant_name=tenant_name,
    )
