"""OIDC / SSO authorization-code client (confidential, PKCE + nonce).

Adapts the standard discovery → authorize → callback → token-exchange flow with
the hardenings a from-scratch implementation should not skip:

  * PKCE (S256) — binds the authorization code to this browser, defeating code
    interception even for a confidential client.
  * nonce — binds the id_token to this exact login request, so a replayed
    id_token from another session is detected.
  * id_token verification — signature via the IdP's JWKS, algorithm pinned to an
    asymmetric allowlist (never `none`/HS), plus issuer + audience + required
    claims, even though the token arrived over TLS.
  * confused-deputy check — `userinfo.sub` must equal `id_token.sub`.
  * email_verified gate — provisioning/linking only trusts a verified email.
  * single-use, TTL'd state — the state entry is popped on callback.

The client is synchronous to match the rest of the API (sync endpoints) and is
held as a singleton on AppDependencies; `_states` is guarded by a lock because
sync endpoints run in a threadpool.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import jwt as pyjwt

from aakaar.core.config import Settings

logger = logging.getLogger(__name__)

_ALLOWED_ID_TOKEN_ALGS = (
    "RS256", "RS384", "RS512",
    "ES256", "ES384", "ES512",
    "PS256", "PS384", "PS512",
)
_DISCOVERY_TTL = 3600.0
_STATE_TTL = 600.0
_HTTP_TIMEOUT = 10.0


class OidcError(Exception):
    """Any failure in the OIDC flow (config, network, or verification)."""


@dataclass(frozen=True, slots=True)
class OidcResult:
    oidc_subject: str  # canonical "{issuer}::{sub}"
    email: str | None
    email_verified: bool
    amr: tuple[str, ...]
    tenant_slug: str | None
    next: str | None


@dataclass
class _State:
    nonce: str
    code_verifier: str
    tenant_slug: str | None
    next: str | None
    created_at: float


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _sanitize_next(value: str | None) -> str | None:
    """Allow only same-origin absolute paths (defeats open-redirect)."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return None
    return value


class OidcClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._discovery: dict[str, object] | None = None
        self._discovery_at = 0.0
        self._states: dict[str, _State] = {}
        self._lock = threading.Lock()
        self._jwks_client: pyjwt.PyJWKClient | None = None

    def enabled(self) -> bool:
        s = self._settings
        return bool(
            s.oidc_enabled
            and s.oidc_issuer
            and s.oidc_client_id
            and s.oidc_client_secret
            and s.oidc_redirect_uri
        )

    # ---- discovery --------------------------------------------------------

    def _discover(self) -> dict[str, object]:
        now = time.time()
        if self._discovery is not None and now - self._discovery_at < _DISCOVERY_TTL:
            return self._discovery
        issuer = str(self._settings.oidc_issuer).rstrip("/")
        url = f"{issuer}/.well-known/openid-configuration"
        try:
            resp = httpx.get(url, timeout=_HTTP_TIMEOUT)
            resp.raise_for_status()
            doc: dict[str, object] = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise OidcError(f"OIDC discovery failed: {e}") from e
        self._discovery = doc
        self._discovery_at = now
        self._jwks_client = pyjwt.PyJWKClient(str(doc["jwks_uri"]))
        return doc

    # ---- login ------------------------------------------------------------

    def begin_login(self, *, tenant_slug: str | None, next_path: str | None) -> str:
        if not self.enabled():
            raise OidcError("OIDC is not configured")
        disc = self._discover()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        challenge = _b64url(hashlib.sha256(code_verifier.encode()).digest())
        with self._lock:
            self._sweep()
            self._states[state] = _State(
                nonce=nonce,
                code_verifier=code_verifier,
                tenant_slug=tenant_slug or self._settings.oidc_default_tenant_slug,
                next=_sanitize_next(next_path),
                created_at=time.time(),
            )
        params = {
            "response_type": "code",
            "client_id": self._settings.oidc_client_id,
            "redirect_uri": self._settings.oidc_redirect_uri,
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{disc['authorization_endpoint']}?{urlencode(params)}"

    # ---- callback ---------------------------------------------------------

    def complete(self, *, code: str, state: str) -> OidcResult:
        with self._lock:
            self._sweep()
            entry = self._states.pop(state, None)  # single-use
        if entry is None:
            raise OidcError("unknown or expired state")

        disc = self._discover()
        token_resp = self._exchange_code(disc, code, entry.code_verifier)
        id_token = token_resp.get("id_token")
        access_token = token_resp.get("access_token")
        if not id_token or not access_token:
            raise OidcError("token endpoint did not return id_token/access_token")

        claims = self._verify_id_token(str(id_token), entry.nonce)
        userinfo = self._userinfo(disc, str(access_token))
        if userinfo.get("sub") != claims.get("sub"):
            raise OidcError("userinfo.sub does not match id_token.sub")

        issuer = str(self._settings.oidc_issuer).rstrip("/")
        sub = str(claims["sub"])
        email = claims.get("email") or userinfo.get("email")
        email_verified = bool(claims.get("email_verified") or userinfo.get("email_verified"))
        amr = self._normalize_amr(claims.get("amr"))
        return OidcResult(
            oidc_subject=f"{issuer}::{sub}",
            email=str(email) if email else None,
            email_verified=email_verified,
            amr=amr,
            tenant_slug=entry.tenant_slug,
            next=entry.next,
        )

    # ---- internals --------------------------------------------------------

    def _exchange_code(
        self, disc: dict[str, object], code: str, verifier: str
    ) -> dict[str, object]:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._settings.oidc_redirect_uri,
            "client_id": self._settings.oidc_client_id,
            "client_secret": self._settings.oidc_client_secret,
            "code_verifier": verifier,
        }
        try:
            resp = httpx.post(
                str(disc["token_endpoint"]), data=data, timeout=_HTTP_TIMEOUT
            )
            resp.raise_for_status()
            out: dict[str, object] = resp.json()
            return out
        except (httpx.HTTPError, ValueError) as e:
            raise OidcError(f"token exchange failed: {e}") from e

    def _verify_id_token(self, id_token: str, nonce: str) -> dict[str, object]:
        if self._jwks_client is None:
            self._discover()
        assert self._jwks_client is not None
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(id_token)
            claims = pyjwt.decode(
                id_token,
                signing_key.key,
                algorithms=list(_ALLOWED_ID_TOKEN_ALGS),
                audience=self._settings.oidc_client_id,
                issuer=str(self._settings.oidc_issuer).rstrip("/"),
                options={"require": ["iss", "aud", "exp", "iat", "sub"]},
            )
        except pyjwt.InvalidTokenError as e:
            raise OidcError(f"id_token verification failed: {e}") from e
        if claims.get("nonce") != nonce:
            raise OidcError("id_token nonce mismatch")
        return claims

    def _userinfo(self, disc: dict[str, object], access_token: str) -> dict[str, object]:
        endpoint = disc.get("userinfo_endpoint")
        if not endpoint:
            return {}
        try:
            resp = httpx.get(
                str(endpoint),
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            info: dict[str, object] = resp.json()
            return info
        except (httpx.HTTPError, ValueError) as e:
            raise OidcError(f"userinfo fetch failed: {e}") from e

    @staticmethod
    def _normalize_amr(raw: object) -> tuple[str, ...]:
        known = {"mfa", "totp", "otp", "hwk", "u2f", "fpt", "pin", "sms"}
        if isinstance(raw, str):
            raw = [raw]
        methods = (
            tuple(m for m in raw if isinstance(m, str) and m in known)
            if isinstance(raw, list)
            else ()
        )
        return ("oidc", *methods)

    def _sweep(self) -> None:
        cutoff = time.time() - _STATE_TTL
        for k in [k for k, v in self._states.items() if v.created_at < cutoff]:
            self._states.pop(k, None)
