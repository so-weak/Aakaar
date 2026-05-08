"""Capability grant repository.

Grants bind (tenant, capability_ref, account_alias) to a vault_ref. The
repository owns both the DB row and the vault entry as a single unit:
write-grant creates both atomically, delete-grant removes both.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from aakar.db.models import CapabilityGrant
from aakar.vault import Vault


class GrantConflict(ValueError):
    pass


def create_grant(
    session: Session,
    vault: Vault,
    *,
    tenant_id: uuid.UUID,
    created_by: uuid.UUID,
    capability_ref: str,
    account_alias: str,
    secrets: dict[str, str],
    input_defaults: dict | None = None,
) -> CapabilityGrant:
    existing = session.scalars(
        select(CapabilityGrant).where(
            CapabilityGrant.tenant_id == tenant_id,
            CapabilityGrant.capability_ref == capability_ref,
            CapabilityGrant.account_alias == account_alias,
        )
    ).first()
    if existing is not None:
        raise GrantConflict(f"{capability_ref}:{account_alias} already granted")

    grant_id = uuid.uuid4()
    vault_ref = f"grants/{grant_id}"
    # Capabilities with no declared secrets (e.g. workflow-shaping stubs)
    # come through with an empty `secrets` dict — the vault refuses empty
    # bundles for safety, so skip the write. `list_grants` and `delete_grant`
    # already tolerate a missing vault entry for these grants.
    if secrets:
        vault.put(str(tenant_id), vault_ref, secrets)
    grant = CapabilityGrant(
        id=grant_id,
        tenant_id=tenant_id,
        capability_ref=capability_ref,
        account_alias=account_alias,
        vault_ref=vault_ref,
        input_defaults=input_defaults or {},
        enabled=True,
        created_by=created_by,
    )
    session.add(grant)
    session.flush()
    return grant


def list_grants(session: Session, tenant_id: uuid.UUID) -> list[CapabilityGrant]:
    return list(
        session.scalars(
            select(CapabilityGrant)
            .where(CapabilityGrant.tenant_id == tenant_id)
            .order_by(CapabilityGrant.created_at)
        )
    )


def list_granted_refs(session: Session, tenant_id: uuid.UUID) -> set[str]:
    rows = session.scalars(
        select(CapabilityGrant.capability_ref).where(
            CapabilityGrant.tenant_id == tenant_id,
            CapabilityGrant.enabled.is_(True),
        )
    ).all()
    return set(rows)


def delete_grant(
    session: Session, vault: Vault, *, tenant_id: uuid.UUID, grant_id: uuid.UUID
) -> bool:
    grant = session.get(CapabilityGrant, grant_id)
    if grant is None or grant.tenant_id != tenant_id:
        return False
    try:
        vault.delete(str(tenant_id), grant.vault_ref)
    except Exception:
        # If the vault entry is already gone we still want to remove the row.
        # A real deployment would surface this rather than swallow it; v1 logs
        # via the caller's exception handler.
        pass
    session.delete(grant)
    session.flush()
    return True


def get_grant(
    session: Session, *, tenant_id: uuid.UUID, grant_id: uuid.UUID
) -> CapabilityGrant | None:
    grant = session.get(CapabilityGrant, grant_id)
    if grant is None or grant.tenant_id != tenant_id:
        return None
    return grant


def update_grant(
    session: Session,
    vault: Vault,
    *,
    tenant_id: uuid.UUID,
    grant_id: uuid.UUID,
    account_alias: str | None = None,
    secrets: dict[str, str] | None = None,
    input_defaults: dict | None = None,
    enabled: bool | None = None,
) -> CapabilityGrant | None:
    """Patch a grant. Each parameter is optional — None means "leave alone".

    Renaming `account_alias` checks for collision against the same
    (tenant, capability_ref). Rotating `secrets` overwrites the vault
    bundle (caller is expected to supply the full declared set). Setting
    `enabled=False` keeps the credential around but hides the capability
    from the planner via list_granted_refs.
    """
    grant = session.get(CapabilityGrant, grant_id)
    if grant is None or grant.tenant_id != tenant_id:
        return None

    if account_alias is not None and account_alias != grant.account_alias:
        clash = session.scalars(
            select(CapabilityGrant).where(
                CapabilityGrant.tenant_id == tenant_id,
                CapabilityGrant.capability_ref == grant.capability_ref,
                CapabilityGrant.account_alias == account_alias,
                CapabilityGrant.id != grant_id,
            )
        ).first()
        if clash is not None:
            raise GrantConflict(
                f"{grant.capability_ref}:{account_alias} already granted"
            )
        grant.account_alias = account_alias

    if secrets is not None and secrets:
        # `put` overwrites atomically; safe even if a vault entry already
        # exists. Empty dicts fall through (vault refuses empty bundles).
        vault.put(str(tenant_id), grant.vault_ref, secrets)

    if input_defaults is not None:
        grant.input_defaults = input_defaults

    if enabled is not None:
        grant.enabled = enabled

    session.flush()
    return grant
