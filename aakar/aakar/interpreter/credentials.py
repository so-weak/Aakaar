"""Vault-backed credential resolution for capabilities + action primitives.

Lives in `interpreter/` (not `capabilities/`) so action primitives like
`browser.fill_secret` can reach it without creating an import cycle.
`capabilities._base` re-exports the same function for back-compat.
"""

from __future__ import annotations

from aakar.interpreter.activities.types import ActivityContext
from aakar.vault import VaultNotFound


def fetch_credentials(
    ctx: ActivityContext, *, capability_ref: str, account_alias: str
) -> dict[str, str]:
    """Resolve a (capability_ref, account_alias) grant to its actual secrets.

    Raises `PermissionError` if the grant is missing, revoked, or its vault
    entry has been deleted. Callers are expected to surface the error as a
    failure event; the orchestrator's run-end cleanup handles browser
    sessions opened before the failure.
    """
    grants_for_cap = ctx.granted_capabilities.get(capability_ref) or {}
    grant = grants_for_cap.get(account_alias)
    if grant is None:
        raise PermissionError(
            f"no grant for capability {capability_ref!r} alias {account_alias!r} "
            f"in this tenant"
        )
    vault_ref = grant["vault_ref"]
    try:
        return ctx.vault.fetch(str(ctx.tenant_id), vault_ref)
    except VaultNotFound as e:
        raise PermissionError(f"vault entry missing for grant {vault_ref!r}") from e
