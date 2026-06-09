"""OIDC / SSO routes.

  GET /auth/oidc/login     — redirect the browser to the IdP (302).
  GET /auth/oidc/callback  — handle the IdP redirect: verify, provision/link a
                             local user, mint an access token, and hand it to
                             the SPA in the URL fragment (never the query string,
                             so it doesn't reach proxies/logs/Referer). Pass
                             ?response=json for a JSON body instead (headless).

OIDC users get a random local password they can't use, so they can only ever
authenticate through the IdP.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aakaar.api.auth import KeyStore, mint_access_token
from aakaar.api.auth.oidc import OidcClient, OidcError, OidcResult
from aakaar.api.deps import get_audit, get_key_store, get_oidc, get_session, get_settings
from aakaar.api.repositories import users as users_repo
from aakaar.api.schemas import LoginResponse
from aakaar.core.config import Settings
from aakaar.db.models import Tenant, User, UserRole, UserStatus
from aakaar.db.tenancy import system_scope
from aakaar.services.audit import AuditRecorder

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/oidc", tags=["auth"])


@router.get("/login")
def oidc_login(
    oidc: Annotated[OidcClient, Depends(get_oidc)],
    tenant: Annotated[str | None, Query()] = None,
    next: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    if not oidc.enabled():
        raise HTTPException(status_code=404, detail="OIDC is not configured")
    try:
        url = oidc.begin_login(tenant_slug=tenant, next_path=next)
    except OidcError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


@router.get("/callback", response_model=None)
def oidc_callback(
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    key_store: Annotated[KeyStore | None, Depends(get_key_store)],
    oidc: Annotated[OidcClient, Depends(get_oidc)],
    audit: Annotated[AuditRecorder, Depends(get_audit)],
    code: Annotated[str, Query()],
    state: Annotated[str, Query()],
    response: Annotated[str | None, Query()] = None,
) -> RedirectResponse | LoginResponse:
    if not oidc.enabled():
        raise HTTPException(status_code=404, detail="OIDC is not configured")
    try:
        result = oidc.complete(code=code, state=state)
    except OidcError as e:
        logger.info("oidc callback failed: %s", e)
        raise HTTPException(status_code=401, detail=f"OIDC login failed: {e}") from e

    user = _resolve_or_provision(session, settings, result)
    issued = datetime.now(UTC)
    ttl = timedelta(minutes=settings.access_token_ttl_minutes)
    token = mint_access_token(
        settings,
        key_store,
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
        amr=result.amr,
        ttl=ttl,
        now=issued,
    )
    user.last_login_at = issued
    session.commit()

    tenant_slug, tenant_name = None, None
    if user.tenant_id is not None:
        with system_scope():
            t = session.get(Tenant, user.tenant_id)
        if t is not None:
            tenant_slug, tenant_name = t.slug, t.name

    audit.record(
        action="auth.login",
        tenant_id=user.tenant_id,
        actor_id=user.id,
        target_kind="user",
        target_id=str(user.id),
        payload={"role": user.role, "amr": "+".join(result.amr)},
    )

    payload = LoginResponse(
        access_token=token,
        expires_at=issued + ttl,
        tenant_slug=tenant_slug,
        tenant_name=tenant_name,
    )
    if response == "json":
        return payload

    fragment = urlencode(
        {
            "access_token": token,
            "expires_at": int((issued + ttl).timestamp()),
            "tenant_slug": tenant_slug or "",
            "next": result.next or "",
        }
    )
    return RedirectResponse(
        f"{settings.oidc_frontend_callback_path}#{fragment}",
        status_code=status.HTTP_302_FOUND,
    )


def _resolve_or_provision(
    session: Session, settings: Settings, result: OidcResult
) -> User:
    """Find the user behind this federated identity, linking or provisioning."""
    with system_scope():
        existing = session.scalars(
            select(User).where(User.oidc_subject == result.oidc_subject)
        ).first()
        if existing is not None:
            if existing.status != UserStatus.ACTIVE:
                raise HTTPException(status_code=403, detail="account disabled")
            return existing

        tenant = _resolve_tenant(session, result.tenant_slug)

        # Optional account linking by verified email.
        if settings.oidc_link_by_verified_email and result.email_verified and result.email:
            match = session.scalars(
                select(User).where(
                    User.tenant_id == tenant.id, User.email == result.email
                )
            ).first()
            if match is not None:
                match.oidc_subject = result.oidc_subject
                session.flush()
                return match

        if not result.email:
            raise HTTPException(status_code=400, detail="IdP did not return an email")
        try:
            user = users_repo.create_user(
                session,
                tenant_id=tenant.id,
                email=result.email,
                password=secrets.token_urlsafe(32),
                role=UserRole.TENANT_USER,
            )
            user.oidc_subject = result.oidc_subject
            session.flush()
            return user
        except IntegrityError:
            # Lost a race with a concurrent first login — re-read the winner.
            session.rollback()
            with system_scope():
                winner = session.scalars(
                    select(User).where(User.oidc_subject == result.oidc_subject)
                ).first()
            if winner is None:
                raise
            return winner


def _resolve_tenant(session: Session, slug: str | None) -> Tenant:
    if not slug:
        raise HTTPException(
            status_code=400,
            detail="no tenant for first OIDC login; pass ?tenant=<slug> or set a default",
        )
    tenant = session.scalars(select(Tenant).where(Tenant.slug == slug)).first()
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"unknown tenant: {slug}")
    return tenant
