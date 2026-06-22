"""Sealed-box transport for the agent back-channel.

The rendezvous broker relays frame bodies VERBATIM (auth is end-to-end but the
broker host sees every byte). Once the agent runs the credential/browser stack,
those bytes include banking credentials and downloaded statements. So we seal
the sensitive payloads — the ``secrets`` envelope (server -> agent) and object
bodies in both directions — to the recipient's public key, so the broker relays
ciphertext only.

Anonymous public-key encryption (libsodium ``SealedBox`` via PyNaCl): anyone can
encrypt to a public key; only the private-key holder decrypts. Each side:
  - generates a keypair at startup and advertises its public key (agent in
    ``hello``, server in ``welcome``),
  - seals a payload to the PEER's public key,
  - unseals with its OWN private key.

PyNaCl is optional at import time: if it (or a peer public key) is absent,
``Sealer`` degrades to a no-op and callers fall back to cleartext with a loud
warning — so a partial rollout never hard-fails, but an operator sees it.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:  # pragma: no cover - exercised by presence/absence in deployments
    from nacl.public import PrivateKey, PublicKey, SealedBox

    _NACL = True
except Exception:  # noqa: BLE001
    _NACL = False


def available() -> bool:
    """True when the crypto backend is importable (PyNaCl installed)."""
    return _NACL


def is_sealed(value: object) -> bool:
    return isinstance(value, dict) and value.get("__sealed__") is True


class Sealer:
    """Holds one keypair. Seals to a peer public key; unseals with own private
    key. A Sealer with no private key (crypto unavailable) is a no-op."""

    def __init__(self, private_key: object | None = None) -> None:
        self._priv = private_key

    @classmethod
    def generate(cls) -> Sealer:
        if not _NACL:
            logger.warning(
                "sealing: PyNaCl not installed — back-channel secrets/blobs will "
                "be sent in CLEARTEXT. Install aakaar-capabilities to enable sealing."
            )
            return cls(None)
        return cls(PrivateKey.generate())

    @property
    def enabled(self) -> bool:
        return self._priv is not None

    def public_key_hex(self) -> str | None:
        if self._priv is None:
            return None
        return bytes(self._priv.public_key).hex()

    def seal(self, plaintext: bytes, peer_public_hex: str | None) -> dict | None:
        """Return a sealed envelope for ``plaintext`` encrypted to the peer, or
        None when sealing isn't possible (no crypto / no peer key) — the caller
        then falls back to a cleartext field."""
        if not _NACL or not peer_public_hex:
            return None
        box = SealedBox(PublicKey(bytes.fromhex(peer_public_hex)))
        return {"__sealed__": True, "ct": box.encrypt(plaintext).hex()}

    def unseal(self, envelope: dict) -> bytes:
        """Decrypt a sealed envelope with our private key."""
        if self._priv is None:
            raise RuntimeError("cannot unseal: no private key / PyNaCl unavailable")
        box = SealedBox(self._priv)
        return box.decrypt(bytes.fromhex(envelope["ct"]))


__all__ = ["Sealer", "available", "is_sealed"]
