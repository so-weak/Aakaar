"""Vault KeyProvider: the default local keys and the external-KMS envelope seam."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from aakaar.vault.key_provider import EnvelopeKeyProvider, LocalKeyProvider
from aakaar.vault.types import VaultError


def _key() -> str:
    return Fernet.generate_key().decode()


def test_local_provider_active_is_first_and_blanks_dropped() -> None:
    k1, k2 = _key(), _key()
    p = LocalKeyProvider([k1, "", k2])  # smuggled blank must be dropped
    assert p.get_active_key() == k1
    assert list(p.decryption_keys()) == [k1, k2]


def test_local_provider_empty_means_no_key() -> None:
    p = LocalKeyProvider([])
    assert p.get_active_key() is None
    assert list(p.decryption_keys()) == []


def test_envelope_unwraps_active_via_injected_kms() -> None:
    data_key = _key()
    # The "wrapped" blob is opaque to us; the injected unwrap_fn is the KMS.
    p = EnvelopeKeyProvider(wrapped_data_key=b"wrapped::active", unwrap_fn=lambda _b: data_key)
    assert p.get_active_key() == data_key
    assert list(p.decryption_keys()) == [data_key]


def test_envelope_includes_previous_keys_for_rotation() -> None:
    active, old = _key(), _key()
    mapping = {b"new": active, b"old": old}
    p = EnvelopeKeyProvider(
        wrapped_data_key=b"new",
        previous_wrapped_data_keys=[b"old"],
        unwrap_fn=lambda b: mapping[b],
    )
    # active first (MultiFernet order), retired key still decryptable.
    assert list(p.decryption_keys()) == [active, old]


def test_envelope_fatal_when_active_unwrap_fails() -> None:
    def boom(wrapped: bytes) -> str:
        raise RuntimeError("KMS denied")

    with pytest.raises(VaultError):
        EnvelopeKeyProvider(wrapped_data_key=b"x", unwrap_fn=boom)


def test_envelope_skips_revoked_previous_key_but_keeps_active() -> None:
    active = _key()

    def unwrap(wrapped: bytes) -> str:
        if wrapped == b"active":
            return active
        raise RuntimeError("revoked")  # the old key no longer unwraps

    p = EnvelopeKeyProvider(
        wrapped_data_key=b"active",
        previous_wrapped_data_keys=[b"revoked-old"],
        unwrap_fn=unwrap,
    )
    # The revoked previous key is skipped, not fatal; active still works.
    assert list(p.decryption_keys()) == [active]


def test_envelope_empty_unwrap_is_fatal_for_active() -> None:
    with pytest.raises(VaultError):
        EnvelopeKeyProvider(wrapped_data_key=b"x", unwrap_fn=lambda _b: "")
