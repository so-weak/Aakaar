"""Pluggable key material for the vault — the KMS integration seam.

``LocalVault`` no longer reads Fernet keys directly; it asks a ``KeyProvider``
for them. That indirection is the whole point of this module: a bank can wire
THEIR key manager (an HSM, a cloud KMS, an on-prem secrets daemon) behind the
``KeyProvider`` Protocol WITHOUT us shipping any of that infrastructure — which
keeps the platform inside its hard "plain-PyPI, no third-party infra"
constraint while still allowing an external root of trust.

Two providers ship:

  - :class:`LocalKeyProvider` — the default. Reads the same comma-separated
    Fernet keys from settings that the vault used before (``AAKAAR_VAULT_KEY``):
    the first is the active write key, the rest are old keys kept decryptable
    during rotation. Behaviour is byte-for-byte what shipped, so existing
    vault files and tests are unaffected.

  - :class:`EnvelopeKeyProvider` — a SCAFFOLD (not wired by default) showing how
    an external KMS slots in via *envelope encryption*: a data key is stored
    locally wrapped (encrypted) by a master key the KMS holds and never
    releases. We never call a cloud SDK — the unwrap is a pluggable callable the
    integrator supplies (``unwrap_fn``), so the seam is constraint-compatible
    and unit-testable with a fake. The unwrapped data key is itself a Fernet
    key, so the rest of the vault is unchanged.

A provider returns raw Fernet key strings (urlsafe-base64, 32 bytes). The
"active" key (``get_active_key``) encrypts new writes; ``decryption_keys()``
returns every key still accepted for decryption, active first (MultiFernet
order). Returning ``None``/empty means "no key configured" — the vault then
either stores plaintext (with a warning) or, under
``AAKAAR_VAULT_REQUIRE_ENCRYPTION``, fails closed.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

from aakaar.vault.types import VaultError

logger = logging.getLogger(__name__)


class KeyProvider(Protocol):
    """Source of Fernet key material for at-rest vault encryption.

    Implementations must be cheap to call (the vault may ask per build) and must
    never log or stringify key material. ``get_active_key`` and
    ``decryption_keys`` should be internally consistent: the active key, when
    set, must be the first element of ``decryption_keys`` so a value written now
    is decryptable now.
    """

    def get_active_key(self) -> str | None:
        """The key that encrypts NEW writes, or ``None`` if no key is configured."""
        ...

    def decryption_keys(self) -> Sequence[str]:
        """Every key accepted for decryption, active first (MultiFernet order).

        Empty when no key is configured. Includes the active key plus any
        retired keys still needed to read values written before a rotation.
        """
        ...


class LocalKeyProvider:
    """Default provider: Fernet keys straight from settings (``AAKAAR_VAULT_KEY``).

    The first key is active (encrypts new writes); the rest are retired keys
    retained for decryption during a rotation window. This reproduces exactly
    what ``LocalVault`` did before the KeyProvider refactor, so existing vault
    files keep decrypting and the vault test-suite is unchanged.
    """

    def __init__(self, keys: Sequence[str] = ()) -> None:
        # Copy + drop blanks so an env value like "k1,,k2" or a trailing comma
        # can't smuggle an empty string into the Fernet builder (which would
        # raise an opaque error far from here).
        self._keys: tuple[str, ...] = tuple(k for k in keys if k)

    def get_active_key(self) -> str | None:
        return self._keys[0] if self._keys else None

    def decryption_keys(self) -> Sequence[str]:
        return self._keys


class EnvelopeKeyProvider:
    """SCAFFOLD: external-KMS envelope encryption (NOT wired by default).

    Demonstrates the integration seam for a bank's own KMS without us depending
    on any cloud SDK. Envelope model:

      - A *data key* (a Fernet key) does the actual at-rest encryption.
      - The data key is never stored in the clear: it is held *wrapped* —
        encrypted by a *master key* that lives in the KMS/HSM and never leaves
        it. We persist only the wrapped blob (e.g. alongside config).
      - To use the vault, the wrapped blob is sent to the KMS to be unwrapped;
        the cleartext data key comes back and is used in-process.

    The KMS call is injected as ``unwrap_fn`` (``wrapped_bytes -> data_key_str``)
    so this class has zero third-party imports and is exercised in tests with a
    fake unwrap. A real deployment supplies a small adapter that calls its KMS's
    ``Decrypt``/unwrap API.

    Key rotation maps onto the same MultiFernet window: pass the current
    ``wrapped_data_key`` plus any ``previous_wrapped_data_keys``; each is
    unwrapped and offered for decryption, current first.

    Unwrap is performed once at construction (eagerly) and cached: the vault is
    long-lived and we must not phone the KMS on every secret read. A failure to
    unwrap the *active* key is fatal (raised) — booting with an unusable write
    key would silently degrade to a fail-closed/plaintext path. Failures
    unwrapping a *previous* key are logged and skipped, since a since-revoked
    old key legitimately stops unwrapping.
    """

    def __init__(
        self,
        *,
        wrapped_data_key: bytes,
        unwrap_fn: UnwrapFn,
        previous_wrapped_data_keys: Sequence[bytes] = (),
    ) -> None:
        active = self._unwrap(unwrap_fn, wrapped_data_key, required=True)
        # mypy: _unwrap(required=True) never returns None.
        assert active is not None
        keys: list[str] = [active]
        for blob in previous_wrapped_data_keys:
            old = self._unwrap(unwrap_fn, blob, required=False)
            if old is not None and old not in keys:
                keys.append(old)
        self._keys: tuple[str, ...] = tuple(keys)

    @staticmethod
    def _unwrap(unwrap_fn: UnwrapFn, blob: bytes, *, required: bool) -> str | None:
        try:
            data_key = unwrap_fn(blob)
        except Exception as e:
            if required:
                # Never echo wrapped bytes or the cause's repr — it may carry
                # key-shaped material in some KMS client errors.
                raise VaultError(
                    "EnvelopeKeyProvider: failed to unwrap the active data key "
                    "via the configured KMS unwrap function"
                ) from None
            logger.warning(
                "EnvelopeKeyProvider: skipping a previous wrapped data key that "
                "failed to unwrap (revoked/rotated out?): %s",
                type(e).__name__,
            )
            return None
        if not data_key:
            if required:
                raise VaultError(
                    "EnvelopeKeyProvider: KMS unwrap returned an empty data key"
                )
            return None
        return data_key

    def get_active_key(self) -> str | None:
        return self._keys[0] if self._keys else None

    def decryption_keys(self) -> Sequence[str]:
        return self._keys


# Signature an integrator implements to bridge their KMS: take the wrapped
# (KMS-encrypted) data-key bytes, return the cleartext Fernet data key string.
# Kept as a module-level alias so adapters can type against it.
class UnwrapFn(Protocol):
    def __call__(self, wrapped: bytes) -> str: ...


__all__ = [
    "EnvelopeKeyProvider",
    "KeyProvider",
    "LocalKeyProvider",
    "UnwrapFn",
]
