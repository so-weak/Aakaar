"""Filesystem-backed vault — dev/v1 only.

Layout:
    {root}/vault/{tenant_id}/{vault_ref_safe}.json

Files are written with mode 0600 so unprivileged users on the host can't
read them. There is NO encryption-at-rest in v1 — that's the whole reason
this driver is dev-only. Production must swap in a real KMS-backed vault.

`vault_ref` is allowed to contain forward slashes (e.g. `grants/<uuid>`);
they are translated to subdirectories. Path traversal is rejected.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from aakar.vault.types import Secrets, VaultEntry, VaultError, VaultNotFound


class LocalVault:
    def __init__(self, root: Path | str) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def put(self, tenant_id: str, vault_ref: str, secrets: Secrets) -> VaultEntry:
        if not secrets:
            raise VaultError("refusing to write empty secrets bundle")
        path = self._resolve(tenant_id, vault_ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically: tmp file with restrictive mode, then rename.
        tmp = path.with_suffix(".json.tmp")
        # Open with O_CREAT and 0o600 so the file is never world-readable even briefly.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(secrets, f, ensure_ascii=False)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        tmp.replace(path)
        return VaultEntry(vault_ref=vault_ref, secret_names=tuple(sorted(secrets)))

    def fetch(self, tenant_id: str, vault_ref: str) -> Secrets:
        path = self._resolve(tenant_id, vault_ref)
        if not path.is_file():
            raise VaultNotFound(f"{tenant_id}/{vault_ref}")
        with path.open() as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise VaultError(f"vault entry malformed: {tenant_id}/{vault_ref}")
        return {str(k): str(v) for k, v in data.items()}

    def delete(self, tenant_id: str, vault_ref: str) -> None:
        path = self._resolve(tenant_id, vault_ref)
        if not path.is_file():
            raise VaultNotFound(f"{tenant_id}/{vault_ref}")
        path.unlink()

    def describe(self, tenant_id: str, vault_ref: str) -> VaultEntry:
        secrets = self.fetch(tenant_id, vault_ref)
        return VaultEntry(vault_ref=vault_ref, secret_names=tuple(sorted(secrets)))

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
