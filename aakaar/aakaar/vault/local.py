"""Filesystem-backed vault with optional Fernet encryption at rest.

Layout:
    {root}/vault/{tenant_id}/{vault_ref_safe}.json

Files are written with mode 0600 so unprivileged users on the host can't
read them. With AAKAAR_VAULT_KEY set, each entry's secret bundle is stored
as a Fernet-encrypted envelope; without it, entries are plaintext JSON and
a startup warning is emitted (set AAKAAR_VAULT_REQUIRE_ENCRYPTION=1 to
refuse to start instead).

Key rotation: AAKAAR_VAULT_KEY accepts comma-separated keys. The FIRST key
encrypts every new write; the rest are old keys still accepted for decryption
(MultiFernet semantics). Pre-encryption plaintext entries remain readable and
are re-encrypted the next time they are written.

Key SOURCE is pluggable via a :class:`~aakaar.vault.key_provider.KeyProvider`
(default :class:`~aakaar.vault.key_provider.LocalKeyProvider`, which reads the
same ``AAKAAR_VAULT_KEY`` settings). A bank can inject a provider backed by
their own KMS without changing this file — see :mod:`aakaar.vault.key_provider`.

`vault_ref` is allowed to contain forward slashes (e.g. `grants/<uuid>`);
they are translated to subdirectories. Path traversal is rejected.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Sequence
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from aakaar.vault.key_provider import KeyProvider, LocalKeyProvider
from aakaar.vault.types import Secrets, VaultEntry, VaultError, VaultNotFound

logger = logging.getLogger(__name__)

# Envelope marker for encrypted entries. A plaintext bundle is a flat
# {name: value} dict; an encrypted one is {_MARKER_KEY: _MARKER_VALUE,
# "token": <fernet token>}. The "$" prefix keeps the marker out of the
# space of plausible secret names.
_MARKER_KEY = "$aakaar_vault"
_MARKER_VALUE = "fernet.v1"


class LocalVault:
    def __init__(
        self,
        root: Path | str,
        *,
        keys: Sequence[str] = (),
        key_provider: KeyProvider | None = None,
        require_encryption: bool = False,
    ) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        # Key material now comes from a KeyProvider (the KMS seam). When a caller
        # passes one we use it; otherwise we wrap the legacy `keys` argument in a
        # LocalKeyProvider so existing call sites and vault files behave exactly
        # as before. Passing both is a wiring bug — the explicit provider wins
        # but we surface it.
        if key_provider is not None and keys:
            logger.warning(
                "LocalVault: both key_provider and keys= were supplied; "
                "using key_provider and ignoring keys="
            )
        provider = key_provider or LocalKeyProvider(keys)
        self._fernet = self._build_fernet(provider, require_encryption=require_encryption)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def encrypts(self) -> bool:
        """True when new writes are encrypted at rest."""
        return self._fernet is not None

    def put(self, tenant_id: str, vault_ref: str, secrets: Secrets) -> VaultEntry:
        if not secrets:
            raise VaultError("refusing to write empty secrets bundle")
        path = self._resolve(tenant_id, vault_ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._encode(secrets)
        # Write atomically: tmp file with restrictive mode, then rename.
        tmp = path.with_suffix(".json.tmp")
        # Open with O_CREAT and 0o600 so the file is never world-readable even briefly.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        tmp.replace(path)
        # Never log secret values — only ref + the names of the keys.
        logger.info(
            "vault.put tenant=%s ref=%s names=%s encrypted=%s",
            tenant_id,
            vault_ref,
            sorted(secrets),
            self.encrypts,
        )
        return VaultEntry(vault_ref=vault_ref, secret_names=tuple(sorted(secrets)))

    def fetch(self, tenant_id: str, vault_ref: str) -> Secrets:
        path = self._resolve(tenant_id, vault_ref)
        if not path.is_file():
            logger.warning("vault.fetch miss tenant=%s ref=%s", tenant_id, vault_ref)
            raise VaultNotFound(f"{tenant_id}/{vault_ref}")
        with path.open() as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.error("vault.fetch malformed tenant=%s ref=%s", tenant_id, vault_ref)
            raise VaultError(f"vault entry malformed: {tenant_id}/{vault_ref}")
        data = self._decode(data, where=f"{tenant_id}/{vault_ref}")
        logger.debug("vault.fetch ok tenant=%s ref=%s names=%s", tenant_id, vault_ref, sorted(data))
        return {str(k): str(v) for k, v in data.items()}

    def delete(self, tenant_id: str, vault_ref: str) -> None:
        path = self._resolve(tenant_id, vault_ref)
        if not path.is_file():
            raise VaultNotFound(f"{tenant_id}/{vault_ref}")
        path.unlink()
        logger.info("vault.delete tenant=%s ref=%s", tenant_id, vault_ref)

    def describe(self, tenant_id: str, vault_ref: str) -> VaultEntry:
        secrets = self.fetch(tenant_id, vault_ref)
        return VaultEntry(vault_ref=vault_ref, secret_names=tuple(sorted(secrets)))

    # --- encryption ----------------------------------------------------------

    @staticmethod
    def _build_fernet(
        provider: KeyProvider, *, require_encryption: bool
    ) -> MultiFernet | None:
        # decryption_keys() is active-first; the MultiFernet built from it
        # therefore encrypts with the active key and still decrypts anything a
        # retired key wrote. A provider with no key configured (local: empty
        # AAKAAR_VAULT_KEY; envelope: KMS unreachable) returns no keys.
        keys = list(provider.decryption_keys())
        if not keys:
            if require_encryption:
                raise VaultError(
                    "AAKAAR_VAULT_REQUIRE_ENCRYPTION is set but the key provider "
                    "returned no key; refusing to store secrets in plaintext. For the "
                    "default LocalKeyProvider, set AAKAAR_VAULT_KEY. Generate a key "
                    "with: python -c 'from cryptography.fernet import Fernet; "
                    "print(Fernet.generate_key().decode())'"
                )
            logger.warning(
                "vault: key provider returned no key — secrets are stored in PLAINTEXT. "
                "Configure AAKAAR_VAULT_KEY (or AAKAAR_VAULT_REQUIRE_ENCRYPTION=1 to fail closed)."
            )
            return None
        fernets: list[Fernet] = []
        for i, key in enumerate(keys):
            try:
                fernets.append(Fernet(key.encode()))
            except (ValueError, TypeError) as e:
                # Index only — never echo key material into logs/exceptions.
                raise VaultError(
                    f"key provider key #{i + 1} is not a valid Fernet key"
                ) from e
        return MultiFernet(fernets)

    def _encode(self, secrets: Secrets) -> dict[str, str]:
        if self._fernet is None:
            if _MARKER_KEY in secrets:
                # A plaintext bundle containing the envelope marker would be
                # misread as encrypted by a future keyed vault.
                raise VaultError(f"secret name {_MARKER_KEY!r} is reserved")
            return dict(secrets)
        token = self._fernet.encrypt(json.dumps(secrets, ensure_ascii=False).encode())
        return {_MARKER_KEY: _MARKER_VALUE, "token": token.decode()}

    def _decode(self, data: dict[str, object], where: str) -> dict[str, object]:
        if data.get(_MARKER_KEY) != _MARKER_VALUE:
            # Pre-encryption plaintext entry: readable as-is (re-encrypted on
            # the next put). No fail-closed here — the key being configured is
            # what unlocks migration; rejecting old entries would brick grants.
            return data
        if self._fernet is None:
            raise VaultError(
                f"vault entry {where} is encrypted but AAKAAR_VAULT_KEY is not configured"
            )
        token = data.get("token")
        if not isinstance(token, str):
            raise VaultError(f"vault entry malformed: {where}")
        try:
            plaintext = self._fernet.decrypt(token.encode())
        except InvalidToken as e:
            raise VaultError(
                f"cannot decrypt vault entry {where}: no configured key matches "
                "(was the old key dropped from AAKAAR_VAULT_KEY before rotating writes?)"
            ) from e
        try:
            decoded = json.loads(plaintext)
        except json.JSONDecodeError:
            # A valid Fernet token wrapping non-JSON bytes means our own
            # ciphertext was tampered with or corrupted. Drop the cause chain
            # with `from None` so a traceback formatter can't surface
            # JSONDecodeError.doc — which holds the decrypted secret bundle.
            raise VaultError(f"vault entry malformed: {where}") from None
        if not isinstance(decoded, dict):
            raise VaultError(f"vault entry malformed: {where}")
        return decoded

    # --- internals ---------------------------------------------------------

    def _resolve(self, tenant_id: str, vault_ref: str) -> Path:
        if not tenant_id or "/" in tenant_id or tenant_id in (".", ".."):
            raise VaultError(f"invalid tenant_id: {tenant_id!r}")
        if not vault_ref:
            raise VaultError("vault_ref must be non-empty")
        for seg in vault_ref.split("/"):
            if seg in ("", ".", ".."):
                raise VaultError(f"invalid vault_ref segment in {vault_ref!r}")
        base = (self._root / "vault" / tenant_id).resolve()
        path = (base / f"{vault_ref}.json").resolve()
        try:
            path.relative_to(base)
        except ValueError as e:
            raise VaultError("vault_ref escapes tenant root") from e
        return path
