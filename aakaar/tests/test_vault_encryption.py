"""LocalVault encryption at rest: roundtrip, rotation, plaintext migration,
fail-closed startup, and bad-key handling."""

from __future__ import annotations

import json
import traceback
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from aakaar.vault import LocalVault
from aakaar.vault.types import VaultError, VaultNotFound

TENANT = "tenant-a"
REF = "grants/g1"
SECRETS = {"password": "hunter2", "username": "soubhik"}


def _key() -> str:
    return Fernet.generate_key().decode()


def _raw_entry(root: Path) -> dict:
    path = root / "vault" / TENANT / "grants" / "g1.json"
    return json.loads(path.read_text())


def test_encrypt_decrypt_roundtrip_and_no_plaintext_on_disk(tmp_path: Path) -> None:
    vault = LocalVault(tmp_path, keys=(_key(),))
    assert vault.encrypts
    vault.put(TENANT, REF, SECRETS)
    assert vault.fetch(TENANT, REF) == SECRETS

    on_disk = (tmp_path / "vault" / TENANT / "grants" / "g1.json").read_text()
    assert "hunter2" not in on_disk
    assert "soubhik" not in on_disk
    assert _raw_entry(tmp_path)["$aakaar_vault"] == "fernet.v1"


def test_key_rotation_multifernet_semantics(tmp_path: Path) -> None:
    old_key, new_key = _key(), _key()
    LocalVault(tmp_path, keys=(old_key,)).put(TENANT, REF, SECRETS)

    # New key first, old key kept: old entries stay readable.
    rotated = LocalVault(tmp_path, keys=(new_key, old_key))
    assert rotated.fetch(TENANT, REF) == SECRETS

    # Writes use the NEWEST (first) key: a vault knowing only new_key reads
    # the rewrite, while one knowing only old_key cannot.
    rotated.put(TENANT, REF, SECRETS)
    assert LocalVault(tmp_path, keys=(new_key,)).fetch(TENANT, REF) == SECRETS
    with pytest.raises(VaultError, match="cannot decrypt"):
        LocalVault(tmp_path, keys=(old_key,)).fetch(TENANT, REF)


def test_plaintext_entry_migrates_on_next_write(tmp_path: Path) -> None:
    LocalVault(tmp_path).put(TENANT, REF, SECRETS)  # plaintext era
    assert "hunter2" in (tmp_path / "vault" / TENANT / "grants" / "g1.json").read_text()

    vault = LocalVault(tmp_path, keys=(_key(),))
    # Transparent read of the pre-encryption entry...
    assert vault.fetch(TENANT, REF) == SECRETS
    # ...and re-encryption on the next write.
    vault.put(TENANT, REF, SECRETS)
    assert "hunter2" not in (tmp_path / "vault" / TENANT / "grants" / "g1.json").read_text()
    assert vault.fetch(TENANT, REF) == SECRETS


def test_unset_key_keeps_plaintext_behavior(tmp_path: Path) -> None:
    vault = LocalVault(tmp_path)
    assert not vault.encrypts
    vault.put(TENANT, REF, SECRETS)
    assert vault.fetch(TENANT, REF) == SECRETS
    assert _raw_entry(tmp_path) == SECRETS


def test_require_encryption_fails_closed_without_key(tmp_path: Path) -> None:
    with pytest.raises(VaultError, match="AAKAAR_VAULT_KEY"):
        LocalVault(tmp_path, require_encryption=True)
    # With a key it starts fine.
    LocalVault(tmp_path, keys=(_key(),), require_encryption=True)


def test_invalid_key_rejected_without_echoing_it(tmp_path: Path) -> None:
    with pytest.raises(VaultError) as exc:
        LocalVault(tmp_path, keys=("not-a-fernet-key",))
    assert "not-a-fernet-key" not in str(exc.value)


def test_encrypted_entry_unreadable_without_key_config(tmp_path: Path) -> None:
    LocalVault(tmp_path, keys=(_key(),)).put(TENANT, REF, SECRETS)
    with pytest.raises(VaultError, match="not configured"):
        LocalVault(tmp_path).fetch(TENANT, REF)


def test_corrupt_ciphertext_raises_vault_error_without_leaking_plaintext(
    tmp_path: Path,
) -> None:
    # A valid Fernet token can wrap non-JSON bytes only via tampering/corruption
    # of our own ciphertext. _decode must surface a typed VaultError (not a raw
    # json.JSONDecodeError) and must not echo the decrypted plaintext anywhere.
    key = _key()
    LocalVault(tmp_path, keys=(key,)).put(TENANT, REF, SECRETS)

    secret_plaintext = "not-json-but-secret-material"
    raw = _raw_entry(tmp_path)
    raw["token"] = Fernet(key.encode()).encrypt(secret_plaintext.encode()).decode()
    (tmp_path / "vault" / TENANT / "grants" / "g1.json").write_text(json.dumps(raw))

    vault = LocalVault(tmp_path, keys=(key,))
    with pytest.raises(VaultError, match="malformed") as exc:
        vault.fetch(TENANT, REF)
    # Neither the message nor a rendered traceback may carry the plaintext.
    # `from None` drops __cause__ and sets __suppress_context__ so a traceback
    # formatter won't print JSONDecodeError.doc (which holds the secret bundle).
    assert secret_plaintext not in str(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__ is True
    rendered = "".join(
        traceback.format_exception(type(exc.value), exc.value, exc.value.__traceback__)
    )
    assert secret_plaintext not in rendered


def test_missing_entry_still_raises_not_found(tmp_path: Path) -> None:
    vault = LocalVault(tmp_path, keys=(_key(),))
    with pytest.raises(VaultNotFound):
        vault.fetch(TENANT, "grants/missing")


def test_load_settings_parses_vault_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from aakaar.core.config import load_settings

    monkeypatch.chdir(tmp_path)  # no .env pickup
    monkeypatch.setenv("AAKAAR_JWT_SECRET", "x" * 48)
    k1, k2 = _key(), _key()
    monkeypatch.setenv("AAKAAR_VAULT_KEY", f" {k1} , {k2} ")
    monkeypatch.setenv("AAKAAR_VAULT_REQUIRE_ENCRYPTION", "1")
    settings = load_settings()
    assert settings.vault_keys == (k1, k2)
    assert settings.vault_require_encryption is True

    monkeypatch.delenv("AAKAAR_VAULT_KEY")
    monkeypatch.delenv("AAKAAR_VAULT_REQUIRE_ENCRYPTION")
    settings = load_settings()
    assert settings.vault_keys == ()
    assert settings.vault_require_encryption is False
