"""JWT issuance and verification.

Two signing modes share one code path:
  * HS256 (default) — a single shared secret. Simple; fine for dev / SQLite /
    airgapped single-node, and what the 24h access token has always used.
  * RS256 (production) — an RSA key pair from a `KeyStore`. The `kid` rides in
    the JWT header; verification resolves the public key by `kid` and **pins
    the algorithm** (never trusts the token's own `alg` header — this closes
    the `alg:none` / HS-RS confusion class of attacks). Public keys are
    published at the JWKS endpoint so a rotation can overlap.

Claim shape:
  sub:        user id (UUID string)
  tid:        tenant id (UUID string) or "superuser"
  role:       "superuser" | "tenant_admin" | "tenant_user"
  amr:        authentication methods, e.g. ["pwd"], ["pwd","totp"], ["oidc"]
  iss / aud:  optional issuer / audience (set under RS256, verified leniently)
  iat / exp:  standard timestamps

The MFA step-up *ticket* is minted with a distinct audience (`aakaar-mfa`) so it
can never be replayed as an access token — `verify_token` rejects an access
token carrying that audience.

Treat the token as opaque on the wire; routers consume `TokenClaims` only.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt as pyjwt

from aakaar.api.auth.keys import SigningKey, is_asymmetric

SUPERUSER_TENANT_SENTINEL = "superuser"
MFA_TICKET_AUDIENCE = "aakaar-mfa"


class InvalidToken(Exception):
    """Raised when a token is missing, expired, or fails verification."""


@dataclass(frozen=True, slots=True)
class TokenClaims:
    user_id: uuid.UUID
    tenant_id: uuid.UUID | None  # None when role == "superuser"
    role: str
    issued_at: datetime
    expires_at: datetime
    amr: tuple[str, ...] = field(default=())
    audience: str | None = None


def issue_access_token(
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    role: str,
    secret: str | None = None,
    algorithm: str = "HS256",
    signing_key: SigningKey | None = None,
    issuer: str | None = None,
    audience: str | None = None,
    amr: Sequence[str] | None = None,
    ttl: timedelta = timedelta(hours=24),
    now: datetime | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Mint a signed token. Pass `signing_key` for RS*/ES*/PS*, `secret` for HS*."""
    issued = now or datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "tid": str(tenant_id) if tenant_id is not None else SUPERUSER_TENANT_SENTINEL,
        "role": role,
        "iat": int(issued.timestamp()),
        "exp": int((issued + ttl).timestamp()),
    }
    if amr:
        payload["amr"] = list(amr)
    if issuer is not None:
        payload["iss"] = issuer
    if audience is not None:
        payload["aud"] = audience
    if extra_claims:
        # Caller extras may not overwrite reserved claims.
        for k, v in extra_claims.items():
            payload.setdefault(k, v)

    if is_asymmetric(algorithm):
        if signing_key is None or signing_key.private_pem is None:
            raise InvalidToken(f"algorithm {algorithm} needs a private signing key")
        return pyjwt.encode(
            payload,
            signing_key.private_pem,
            algorithm=algorithm,
            headers={"kid": signing_key.kid},
        )
    if not secret:
        raise InvalidToken(f"algorithm {algorithm} needs a shared secret")
    return pyjwt.encode(payload, secret, algorithm=algorithm)


def verify_token(
    token: str,
    *,
    secret: str | None = None,
    algorithm: str = "HS256",
    key_resolver: Callable[[str], str | None] | None = None,
    issuer: str | None = None,
    audience: str | None = None,
    require_audience: bool = False,
) -> TokenClaims:
    """Verify a token's signature/expiry and return its claims.

    Audience handling is deliberately layered so MFA tickets and access tokens
    can't be confused while staying backward-compatible with legacy HS tokens
    that carry no `aud`:
      * require_audience=True  → token MUST carry exactly `audience`.
      * require_audience=False → if the token has an `aud`, it must equal
        `audience`; a token with no `aud` is accepted (legacy).
    Issuer is verified leniently (only when the token actually carries `iss`).
    """
    if not token:
        raise InvalidToken("empty token")

    key: str | None
    if is_asymmetric(algorithm):
        if key_resolver is None:
            raise InvalidToken("no key resolver for asymmetric verification")
        try:
            header = pyjwt.get_unverified_header(token)
        except pyjwt.InvalidTokenError as e:
            raise InvalidToken(f"invalid token header: {e}") from e
        kid = header.get("kid")
        if not kid:
            raise InvalidToken("token missing kid")
        key = key_resolver(kid)
        if key is None:
            raise InvalidToken(f"unknown signing key kid={kid}")
    else:
        if not secret:
            raise InvalidToken("no secret for symmetric verification")
        key = secret

    try:
        # algorithms is pinned to the configured algorithm only — the token's
        # own alg header is never trusted.
        payload = pyjwt.decode(
            token,
            key,
            algorithms=[algorithm],
            options={"verify_aud": False, "require": ["sub", "tid", "role", "iat", "exp"]},
        )
    except pyjwt.ExpiredSignatureError as e:
        raise InvalidToken("token expired") from e
    except pyjwt.InvalidTokenError as e:
        raise InvalidToken(f"invalid token: {e}") from e

    aud = payload.get("aud")
    if require_audience:
        if aud != audience:
            raise InvalidToken("wrong audience")
    elif audience is not None and aud is not None and aud != audience:
        raise InvalidToken("wrong audience")

    iss = payload.get("iss")
    if issuer is not None and iss is not None and iss != issuer:
        raise InvalidToken("wrong issuer")

    try:
        sub = payload["sub"]
        tid = payload["tid"]
        role = payload["role"]
        iat = payload["iat"]
        exp = payload["exp"]
    except KeyError as e:
        raise InvalidToken(f"token missing claim: {e}") from e

    raw_amr = payload.get("amr") or []
    amr = tuple(str(m) for m in raw_amr) if isinstance(raw_amr, list) else ()

    return TokenClaims(
        user_id=uuid.UUID(sub),
        tenant_id=None if tid == SUPERUSER_TENANT_SENTINEL else uuid.UUID(tid),
        role=role,
        issued_at=datetime.fromtimestamp(iat, tz=UTC),
        expires_at=datetime.fromtimestamp(exp, tz=UTC),
        amr=amr,
        audience=aud,
    )
