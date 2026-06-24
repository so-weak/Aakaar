"""Grant cap.shell_exec to the CHANAKYA tenant.

cap.shell_exec runs an allow-listed argv command on a remote agent (e.g. a curl
on ashu-mac) and captures its output. It is a headless shared cap with no
secrets, but capabilities are grant-gated per tenant, so a workflow that uses it
fails DAG validation (HTTP 422) until the tenant holds the grant. This makes it
visible to CHANAKYA only; other tenants are untouched. Idempotent — safe to
re-run.

    cd aakaar && .venv/bin/python -m aakaar.scripts.seed_chanakya_shell_exec

Restart the API afterwards so the grant is picked up by the planner.
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from aakaar.api.repositories import grants as grants_repo
from aakaar.core.config import load_settings
from aakaar.db.models import Tenant, User, UserRole
from aakaar.db.session import EngineConfig, SessionFactory, make_engine
from aakaar.vault import LocalVault, Vault

_TENANT_SLUG = "chanakya"
_CAP_REFS = ("cap.shell_exec",)


def _ensure_grant(session: Session, vault: Vault, *, tenant_id, created_by, cap_ref: str) -> None:
    existing = next(
        (g for g in grants_repo.list_grants(session, tenant_id) if g.capability_ref == cap_ref),
        None,
    )
    if existing is not None:
        if not existing.enabled:
            grants_repo.update_grant(
                session, vault, tenant_id=tenant_id, grant_id=existing.id, enabled=True
            )
            print(f"  re-enabled {cap_ref}")
        else:
            print(f"  {cap_ref} already granted (alias={existing.account_alias})")
        return
    grants_repo.create_grant(
        session,
        vault,  # unused for the write itself: this cap declares no secrets
        tenant_id=tenant_id,
        created_by=created_by,
        capability_ref=cap_ref,
        account_alias="default",
        secrets={},
    )
    print(f"  granted {cap_ref} (alias=default)")


def main() -> int:
    settings = load_settings()
    engine = make_engine(EngineConfig(url=settings.db_url))
    factory = SessionFactory(engine)
    vault = LocalVault(Path(settings.data_dir))
    print(f"Granting cap.shell_exec to {_TENANT_SLUG} against db={settings.db_url}")
    with factory.session() as s:
        tenant = s.scalars(select(Tenant).where(Tenant.slug == _TENANT_SLUG)).first()
        if tenant is None:
            print(f"  ERROR: tenant {_TENANT_SLUG!r} not found", file=sys.stderr)
            return 1
        admin = s.scalars(
            select(User)
            .where(User.tenant_id == tenant.id, User.role == UserRole.TENANT_ADMIN)
            .order_by(User.created_at)
        ).first()
        if admin is None:
            print(f"  ERROR: no tenant_admin user for {_TENANT_SLUG!r}", file=sys.stderr)
            return 1
        for cap_ref in _CAP_REFS:
            _ensure_grant(s, vault, tenant_id=tenant.id, created_by=admin.id, cap_ref=cap_ref)
        s.commit()
    print("\nDone. Restart the API so the grant loads.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
