"""JWKS endpoint — publishes the public half of every RSA signing key.

Mounted at both `/auth/.well-known/jwks.json` (under the rate-limited /auth
prefix) and the conventional `/.well-known/jwks.json`. Returns an empty key set
under HS256 (no key store), so a verifier can probe safely either way.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from aakaar.api.auth import KeyStore
from aakaar.api.deps import get_key_store, get_settings
from aakaar.core.config import Settings

router = APIRouter(tags=["auth"])


def _jwks(key_store: KeyStore | None, settings: Settings) -> dict[str, list[dict[str, str]]]:
    if key_store is None:
        return {"keys": []}
    return key_store.jwks(algorithm=settings.jwt_algorithm)


@router.get("/auth/.well-known/jwks.json")
def jwks_auth(
    key_store: Annotated[KeyStore | None, Depends(get_key_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, list[dict[str, str]]]:
    return _jwks(key_store, settings)


@router.get("/.well-known/jwks.json")
def jwks_wellknown(
    key_store: Annotated[KeyStore | None, Depends(get_key_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, list[dict[str, str]]]:
    return _jwks(key_store, settings)
