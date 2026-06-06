"""JWT issuance and verification.

v1 uses a single short-lived access token (default 24h). No refresh-token
rotation yet — sessions are simple.

Claim shape:
  sub:        user id (UUID string)
  tid:        tenant id (UUID string) or "superuser"
  role:       "superuser" | "tenant_admin" | "tenant_user"
  iat / exp:  standard timestamps

Treat the token as opaque on the wire; routers consume `TokenClaims` only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt as pyjwt

SUPERUSER_TENANT_SENTINEL = "superuser"


class InvalidToken(Exception):
    """Raised when a token is missing, expired, or fails verification."""


@dataclass(frozen=True, slots=True)
class TokenClaims:
    user_id: uuid.UUID
    tenant_id: uuid.UUID | None  # None when role == "superuser"
    role: str
    issued_at: datetime
    expires_at: datetime


def issue_access_token(
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    role: str,
    secret: str,
    algorithm: str = "HS256",
    ttl: timedelta = timedelta(hours=24),
    now: datetime | None = None,
) -> str:
    issued = now or datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "tid": str(tenant_id) if tenant_id is not None else SUPERUSER_TENANT_SENTINEL,
        "role": role,
        "iat": int(issued.timestamp()),
        "exp": int((issued + ttl).timestamp()),
    }
    return pyjwt.encode(payload, secret, algorithm=algorithm)


def verify_token(token: str, *, secret: str, algorithm: str = "HS256") -> TokenClaims:
    if not token:
        raise InvalidToken("empty token")
    try:
        payload = pyjwt.decode(token, secret, algorithms=[algorithm])
    except pyjwt.ExpiredSignatureError as e:
        raise InvalidToken("token expired") from e
    except pyjwt.InvalidTokenError as e:
        raise InvalidToken(f"invalid token: {e}") from e

    try:
        sub = payload["sub"]
        tid = payload["tid"]
        role = payload["role"]
        iat = payload["iat"]
        exp = payload["exp"]
    except KeyError as e:
        raise InvalidToken(f"token missing claim: {e}") from e

    return TokenClaims(
        user_id=uuid.UUID(sub),
        tenant_id=None if tid == SUPERUSER_TENANT_SENTINEL else uuid.UUID(tid),
        role=role,
        issued_at=datetime.fromtimestamp(iat, tz=UTC),
        expires_at=datetime.fromtimestamp(exp, tz=UTC),
    )
