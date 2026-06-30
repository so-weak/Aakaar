"""Grant the CTS-Outward helper capabilities to the CHANAKYA tenant only.

These shared caps were added for the CTS Outward (ExpressClear ZK) flows whose
controls the generic primitives can't drive:

  - cap.web_click       click a control by image/icon asset, text, or selector
                        (the icon-only Logout button).
  - cap.web_select      choose a value in a ZK combobox / native <select> by its
                        field label (Processing Date, Record Type, …).
  - cap.web_tree_select expand + open a ZK tree-menu node by label path
                        (E-Callback Processing -> Ecall Back Processing).

Capabilities are grant-gated per tenant, so this makes them visible to CHANAKYA
only; other tenants' planner and runs are untouched. Idempotent — safe to re-run.

    cd aakaar && .venv/bin/python -m aakaar.scripts.seed_chanakya_caps

Restart the API and the agent afterwards so the caps are registered, advertised,
and surfaced to the planner.
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
_CAP_REFS = (
    "cap.web_click", "cap.web_select", "cap.web_tree_select",
    # CTS cheque-verify flow
    "cap.web_read_field", "cap.ocr_account_number", "cap.value_decision", "cap.csv_report",
    # CTS cheque-verify v2 (batch loop + reject remark)
    "cap.web_fill_field", "cap.cts_cheque_verify_loop",
    # CTS multi-batch sweep (dates/cycles/batches -> consolidated report)
    "cap.cts_batch_sweep",
    # CTS back-image harvest (download all back images named by account -> zip)
    "cap.cts_back_image_harvest",
    # CTS back-image multi-batch sweep (dates/cycles/batches -> consolidated zip)
    "cap.cts_back_image_sweep",
)


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
        vault,  # unused for the write itself: these caps declare no secrets
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
    print(f"Granting CTS-Outward caps to {_TENANT_SLUG} against db={settings.db_url}")
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
    print("\nDone. Restart the API + agent so the caps load, advertise, and reindex.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
