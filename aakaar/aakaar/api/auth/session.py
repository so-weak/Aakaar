"""Token minting/verification glue.

One seam that every login path (password, MFA step-up, OIDC) funnels through,
so the HS-vs-RS decision and the issuer/audience policy live in exactly one
place. Routers call `mint_access_token` / `verify_access_token` and never touch
the key material directly.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import jwt as pyjwt

from aakaar.api.auth.jwt import (
    MFA_TICKET_AUDIENCE,
    TokenClaims,
    issue_access_token,
    verify_token,
)
from aakaar.api.auth.keys import KeyStore, is_asymmetric
from aakaar.core.config import Settings


def mint_access_token(
    settings: Settings,
    key_store: KeyStore | None,
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    role: str,
    amr: Sequence[str],
    ttl: timedelta,
    now: datetime | None = None,
    audience: str | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    alg = settings.jwt_algorithm
    aud = audience or settings.jwt_audience
    if is_asymmetric(alg):
        if key_store is None:
            raise RuntimeError(
                f"jwt_algorithm={alg} but no key store configured (set AAKAAR_JWT_KEY_DIR)"
            )
        return issue_access_token(
            user_id=user_id,
            tenant_id=tenant_id,
            role=role,
            algorithm=alg,
            signing_key=key_store.active(),
            issuer=settings.jwt_issuer,
            audience=aud,
            amr=amr,
            ttl=ttl,
            now=now,
            extra_claims=extra_claims,
        )
    return issue_access_token(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        secret=settings.jwt_secret,
        algorithm=alg,
        issuer=settings.jwt_issuer,
        audience=aud,
        amr=amr,
        ttl=ttl,
        now=now,
        extra_claims=extra_claims,
    )


def verify_access_token(
    token: str, settings: Settings, key_store: KeyStore | None
) -> TokenClaims:
    alg = settings.jwt_algorithm
    if is_asymmetric(alg):
        return verify_token(
            token,
            algorithm=alg,
            key_resolver=key_store.resolver() if key_store else None,
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            require_audience=False,
        )
    return verify_token(
        token,
        secret=settings.jwt_secret,
        algorithm=alg,
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        require_audience=False,
    )


def mfa_binding_hash(*, password_hash: str, status: str, totp_secret: str | None) -> str:
    """Bind an MFA ticket to the user's security-relevant state, so a password
    change / disable / TOTP-secret rotation between password-step and
    code-step invalidates an in-flight ticket."""
    raw = f"{password_hash}|{status}|{totp_secret or ''}".encode()
    return hashlib.sha256(raw).hexdigest()


def mint_mfa_ticket(
    settings: Settings,
    key_store: KeyStore | None,
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    role: str,
    bnd: str,
    ttl: timedelta = timedelta(minutes=5),
    now: datetime | None = None,
) -> str:
    """Short-lived ticket issued after password success when MFA is required.

    Carries a distinct audience (so it can't be replayed as an access token)
    and a `bnd` binding hash of the user's security-relevant fields so that a
    password change / disable / secret rotation invalidates an in-flight ticket.
    """
    return mint_access_token(
        settings,
        key_store,
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        amr=("pwd",),
        ttl=ttl,
        now=now,
        audience=MFA_TICKET_AUDIENCE,
        extra_claims={"bnd": bnd},
    )


def verify_mfa_ticket(
    token: str, settings: Settings, key_store: KeyStore | None
) -> tuple[TokenClaims, str]:
    """Verify an MFA ticket (audience-pinned) and return (claims, bnd)."""
    alg = settings.jwt_algorithm
    if is_asymmetric(alg):
        claims = verify_token(
            token,
            algorithm=alg,
            key_resolver=key_store.resolver() if key_store else None,
            issuer=settings.jwt_issuer,
            audience=MFA_TICKET_AUDIENCE,
            require_audience=True,
        )
    else:
        claims = verify_token(
            token,
            secret=settings.jwt_secret,
            algorithm=alg,
            issuer=settings.jwt_issuer,
            audience=MFA_TICKET_AUDIENCE,
            require_audience=True,
        )
    # Signature already verified above; this second decode only reads `bnd`.
    payload = pyjwt.decode(token, options={"verify_signature": False})
    return claims, str(payload.get("bnd", ""))
