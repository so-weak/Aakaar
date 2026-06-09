"""MFA (TOTP) endpoints.

All live under /auth/* so they inherit the tighter auth rate-limit bucket.

  GET  /auth/mfa/status   — is MFA on / mid-enrollment?            (current user)
  POST /auth/mfa/enroll   — start enrollment, return secret + QR   (current user)
  POST /auth/mfa/confirm  — confirm a code, activate, return       (current user)
                            one-time recovery codes
  POST /auth/mfa/disable  — turn MFA off (requires a current code)  (current user)
  POST /auth/mfa/verify   — finish a login step-up: ticket + code   (no auth;
                            (or recovery code) -> access token       ticket-gated)

Note: confirming MFA invalidates the caller's current ("pwd"-only) token on the
*next* request — the UI should prompt a re-login after showing recovery codes.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from aakaar.api.auth import (
    KeyStore,
    mfa_binding_hash,
    mint_access_token,
    verify_mfa_ticket,
)
from aakaar.api.auth import totp as totp_lib
from aakaar.api.deps import (
    get_audit,
    get_current_user,
    get_key_store,
    get_session,
    get_settings,
)
from aakaar.api.schemas import (
    LoginResponse,
    MfaConfirmRequest,
    MfaConfirmResponse,
    MfaDisableRequest,
    MfaEnrollResponse,
    MfaStatusResponse,
    MfaVerifyRequest,
)
from aakaar.core.config import Settings
from aakaar.db.models import User, UserStatus
from aakaar.db.tenancy import system_scope
from aakaar.services.audit import AuditRecorder

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/mfa", tags=["auth"])


@router.get("/status", response_model=MfaStatusResponse)
def mfa_status(user: Annotated[User, Depends(get_current_user)]) -> MfaStatusResponse:
    return MfaStatusResponse(
        enabled=user.mfa_enabled, pending=user.totp_pending_secret is not None
    )


@router.post("/enroll", response_model=MfaEnrollResponse)
def mfa_enroll(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> MfaEnrollResponse:
    if user.mfa_enabled:
        raise HTTPException(status_code=409, detail="MFA already enabled")
    secret = totp_lib.generate_secret()
    # Store pending (encrypted if a key is configured). Not active until confirm,
    # so an abandoned enrollment can't lock the user out.
    user.totp_pending_secret = totp_lib.protect_secret(secret, settings.mfa_encryption_key)
    session.commit()
    uri = totp_lib.provisioning_uri(secret, account=user.email, issuer=settings.mfa_issuer)
    return MfaEnrollResponse(secret=secret, otpauth_url=uri)


@router.post("/confirm", response_model=MfaConfirmResponse)
def mfa_confirm(
    body: MfaConfirmRequest,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    audit: Annotated[AuditRecorder, Depends(get_audit)],
) -> MfaConfirmResponse:
    if user.totp_pending_secret is None:
        raise HTTPException(status_code=400, detail="no enrollment in progress")
    secret = totp_lib.unprotect_secret(user.totp_pending_secret, settings.mfa_encryption_key)
    step = totp_lib.verify_code(secret, body.code)
    if step is None:
        raise HTTPException(status_code=400, detail="invalid code")
    # Promote pending -> active; mint + store hashed recovery codes.
    recovery = totp_lib.generate_recovery_codes()
    user.totp_secret = user.totp_pending_secret
    user.totp_pending_secret = None
    user.totp_last_step = step
    user.mfa_enabled = True
    user.mfa_recovery_codes = {"codes": totp_lib.hash_recovery_codes(recovery)}
    session.commit()
    audit.record(
        action="auth.mfa_enabled",
        tenant_id=user.tenant_id,
        actor_id=user.id,
        target_kind="user",
        target_id=str(user.id),
    )
    return MfaConfirmResponse(recovery_codes=recovery)


@router.post("/disable", status_code=status.HTTP_204_NO_CONTENT)
def mfa_disable(
    body: MfaDisableRequest,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    audit: Annotated[AuditRecorder, Depends(get_audit)],
) -> None:
    if not user.mfa_enabled or user.totp_secret is None:
        raise HTTPException(status_code=400, detail="MFA is not enabled")
    secret = totp_lib.unprotect_secret(user.totp_secret, settings.mfa_encryption_key)
    if totp_lib.verify_code(secret, body.code, last_step=user.totp_last_step) is None:
        raise HTTPException(status_code=400, detail="invalid code")
    user.mfa_enabled = False
    user.totp_secret = None
    user.totp_pending_secret = None
    user.totp_last_step = None
    user.mfa_recovery_codes = None
    session.commit()
    audit.record(
        action="auth.mfa_disabled",
        tenant_id=user.tenant_id,
        actor_id=user.id,
        target_kind="user",
        target_id=str(user.id),
    )


@router.post("/verify", response_model=LoginResponse)
def mfa_verify(
    body: MfaVerifyRequest,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    key_store: Annotated[KeyStore | None, Depends(get_key_store)],
    audit: Annotated[AuditRecorder, Depends(get_audit)],
) -> LoginResponse:
    from aakaar.api.auth import InvalidToken

    try:
        claims, bnd = verify_mfa_ticket(body.mfa_token, settings, key_store)
    except InvalidToken as e:
        raise HTTPException(status_code=401, detail=f"invalid mfa ticket: {e}") from e

    with system_scope():
        user = session.get(User, claims.user_id)
    if user is None or user.status != UserStatus.ACTIVE or not user.mfa_enabled:
        raise HTTPException(status_code=401, detail="mfa not available for this account")
    # Re-bind: a password change / disable / secret rotation between steps voids
    # the ticket.
    expected = mfa_binding_hash(
        password_hash=user.password_hash, status=user.status, totp_secret=user.totp_secret
    )
    if bnd != expected:
        raise HTTPException(status_code=401, detail="ticket no longer valid")

    ok = False
    if body.code and user.totp_secret is not None:
        secret = totp_lib.unprotect_secret(user.totp_secret, settings.mfa_encryption_key)
        step = totp_lib.verify_code(secret, body.code, last_step=user.totp_last_step)
        if step is not None:
            user.totp_last_step = step
            ok = True
    elif body.recovery_code and user.mfa_recovery_codes:
        remaining = totp_lib.consume_recovery_code(
            list(user.mfa_recovery_codes.get("codes", [])), body.recovery_code
        )
        if remaining is not None:
            user.mfa_recovery_codes = {"codes": remaining}
            ok = True

    if not ok:
        audit.record(
            action="auth.mfa_failed",
            tenant_id=user.tenant_id,
            actor_id=user.id,
            target_kind="user",
            target_id=str(user.id),
        )
        raise HTTPException(status_code=401, detail="invalid code")

    issued = datetime.now(UTC)
    ttl = timedelta(minutes=settings.access_token_ttl_minutes)
    token = mint_access_token(
        settings,
        key_store,
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
        amr=("pwd", "totp"),
        ttl=ttl,
        now=issued,
    )
    user.last_login_at = issued
    session.commit()

    tenant_slug: str | None = None
    tenant_name: str | None = None
    if user.tenant_id is not None:
        from aakaar.db.models import Tenant

        with system_scope():
            tenant = session.get(Tenant, user.tenant_id)
        if tenant is not None:
            tenant_slug, tenant_name = tenant.slug, tenant.name

    audit.record(
        action="auth.login",
        tenant_id=user.tenant_id,
        actor_id=user.id,
        target_kind="user",
        target_id=str(user.id),
        payload={"role": user.role, "amr": "pwd+totp"},
    )
    return LoginResponse(
        access_token=token,
        expires_at=issued + ttl,
        tenant_slug=tenant_slug,
        tenant_name=tenant_name,
    )
