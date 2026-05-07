"""Vault abstraction.

A vault stores per-tenant secret bundles. The DB only carries `vault_ref`
strings (opaque to the application); the worker fetches `Secrets` (a dict
of name -> string) from the vault at execution time.

v1 ships `LocalVault`, a filesystem driver suitable for dev. Production
deployments swap in a real backend (AWS Secrets Manager, HashiCorp Vault)
behind the same Protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class VaultError(Exception):
    """Base class for vault errors."""


class VaultNotFound(VaultError):
    """Raised when fetching a non-existent vault_ref."""


Secrets = dict[str, str]


@dataclass(frozen=True, slots=True)
class VaultEntry:
    vault_ref: str
    secret_names: tuple[str, ...]  # names only; values are NEVER returned by metadata APIs


class Vault(Protocol):
    """Tenant-scoped secret storage.

    `put`/`fetch` deal in (tenant_id, vault_ref) pairs. The vault_ref is
    chosen by the caller — typically `grants/{grant_id}` — and treated as
    opaque by the vault.
    """

    def put(self, tenant_id: str, vault_ref: str, secrets: Secrets) -> VaultEntry: ...

    def fetch(self, tenant_id: str, vault_ref: str) -> Secrets: ...

    def delete(self, tenant_id: str, vault_ref: str) -> None: ...

    def describe(self, tenant_id: str, vault_ref: str) -> VaultEntry: ...
