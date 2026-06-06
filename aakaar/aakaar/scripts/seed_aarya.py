"""Seed the AARYA tenant with the demo flows' prerequisites.

Idempotent: safe to re-run after `truncate_data`. Creates:

  - tenant: AARYA (slug=aarya)
  - admin:  admin@aarya.test / aaryaAdmin1!
  - user:   ops@aarya.test  / aaryaOps1!
  - vault sites:
      - nbbl       → http://localhost:3001/login          admin / nbbl@123
      - hdfc-admin → http://localhost:3000/login          K22408m / hdfc@123
  - capability toggles:
      - cap.file_download
      - cap.file_upload

Run:
    cd aakaar && .venv/bin/python -m aakaar.scripts.seed_aarya

Reads .env to hit the same DB and vault as the running API. After
seeding, restart the API so it drops cached CapabilityIndex state.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

from sqlalchemy import select

from aakaar.api.repositories import grants as grants_repo
from aakaar.api.repositories import tenants as tenants_repo
from aakaar.api.repositories import users as users_repo
from aakaar.core.config import load_settings
from aakaar.db.models import Tenant, User, UserRole
from aakaar.db.session import EngineConfig, SessionFactory, make_engine
from aakaar.vault import LocalVault, Vault

# ---------- desired state ---------------------------------------------------

_TENANT_SLUG = "aarya"
_TENANT_NAME = "AARYA"

_ADMIN_EMAIL = "admin@aarya.test"
_ADMIN_PASSWORD = "aaryaAdmin1!"

_OPS_EMAIL = "ops@aarya.test"
_OPS_PASSWORD = "aaryaOps1!"

# (capability_ref, alias, login_url, display_name, secrets)
_SITES = (
    {
        "capability_ref": "cap.web_login",
        "alias": "nbbl",
        "login_url": "http://localhost:3001/login",
        "display_name": "NBBL portal",
        "secrets": {"username": "admin", "password": "nbbl@123"},
    },
    {
        "capability_ref": "cap.web_login",
        "alias": "hdfc-admin",
        "login_url": "http://localhost:3000/login",
        "display_name": "HDFC admin BOSS",
        "secrets": {"username": "K22408m", "password": "hdfc@123"},
    },
)

_SESSION_BOUND_CAPABILITIES = (
    "cap.file_download",
    "cap.file_upload",
)


# ---------- helpers --------------------------------------------------------


def _ensure_tenant(session, slug: str, name: str) -> Tenant:
    existing = session.scalars(select(Tenant).where(Tenant.slug == slug)).first()
    if existing is not None:
        print(f"  tenant already exists: {slug}")
        return existing
    tenant = tenants_repo.create_tenant(session, slug=slug, name=name)
    print(f"  created tenant: {slug}")
    return tenant


def _ensure_user(
    session, *, tenant_id: uuid.UUID, email: str, password: str, role: str
) -> User:
    existing = session.scalars(
        select(User).where(User.tenant_id == tenant_id, User.email == email)
    ).first()
    if existing is not None:
        print(f"  user already exists: {email}")
        return existing
    user = users_repo.create_user(
        session,
        tenant_id=tenant_id,
        email=email,
        password=password,
        role=role,
    )
    print(f"  created user: {email} ({role})")
    return user


def _ensure_site(
    session,
    vault: Vault,
    *,
    tenant_id: uuid.UUID,
    created_by: uuid.UUID,
    capability_ref: str,
    alias: str,
    login_url: str,
    display_name: str,
    secrets: dict[str, str],
) -> None:
    grants = grants_repo.list_grants(session, tenant_id)
    existing = next(
        (
            g
            for g in grants
            if g.capability_ref == capability_ref and g.account_alias == alias
        ),
        None,
    )
    input_defaults = {"login_url": login_url, "display_name": display_name}

    if existing is None:
        grants_repo.create_grant(
            session,
            vault,
            tenant_id=tenant_id,
            created_by=created_by,
            capability_ref=capability_ref,
            account_alias=alias,
            secrets=secrets,
            input_defaults=input_defaults,
        )
        print(f"  created site: {alias} → {login_url}")
        return

    # Refresh URL/display_name + rotate creds so a re-run cleans up
    # accidental edits in dev.
    grants_repo.update_grant(
        session,
        vault,
        tenant_id=tenant_id,
        grant_id=existing.id,
        secrets=secrets,
        input_defaults=input_defaults,
        enabled=True,
    )
    print(f"  refreshed site: {alias} → {login_url}")


def _ensure_capability_toggle(
    session,
    vault: Vault,
    *,
    tenant_id: uuid.UUID,
    created_by: uuid.UUID,
    capability_ref: str,
) -> None:
    grants = grants_repo.list_grants(session, tenant_id)
    existing = next(
        (g for g in grants if g.capability_ref == capability_ref), None
    )
    if existing is not None:
        if not existing.enabled:
            grants_repo.update_grant(
                session,
                vault,
                tenant_id=tenant_id,
                grant_id=existing.id,
                enabled=True,
            )
            print(f"  re-enabled capability: {capability_ref}")
        else:
            print(f"  capability already enabled: {capability_ref}")
        return
    grants_repo.create_grant(
        session,
        vault,
        tenant_id=tenant_id,
        created_by=created_by,
        capability_ref=capability_ref,
        account_alias="default",
        secrets={},
    )
    print(f"  enabled capability: {capability_ref}")


# ---------- main -----------------------------------------------------------


def main() -> int:
    settings = load_settings()
    engine = make_engine(EngineConfig(url=settings.db_url))
    factory = SessionFactory(engine)
    # LocalVault internally appends "vault/" to its root, so pass the
    # raw data_dir to match the API's construction in api/main.py.
    vault = LocalVault(Path(settings.data_dir))

    print(f"Seeding AARYA against db={settings.db_url}")

    with factory.session() as s:
        tenant = _ensure_tenant(s, _TENANT_SLUG, _TENANT_NAME)
        admin = _ensure_user(
            s,
            tenant_id=tenant.id,
            email=_ADMIN_EMAIL,
            password=_ADMIN_PASSWORD,
            role=UserRole.TENANT_ADMIN,
        )
        _ensure_user(
            s,
            tenant_id=tenant.id,
            email=_OPS_EMAIL,
            password=_OPS_PASSWORD,
            role=UserRole.TENANT_USER,
        )
        for site in _SITES:
            _ensure_site(
                s,
                vault,
                tenant_id=tenant.id,
                created_by=admin.id,
                capability_ref=site["capability_ref"],
                alias=site["alias"],
                login_url=site["login_url"],
                display_name=site["display_name"],
                secrets=site["secrets"],
            )
        for cap_ref in _SESSION_BOUND_CAPABILITIES:
            _ensure_capability_toggle(
                s,
                vault,
                tenant_id=tenant.id,
                created_by=admin.id,
                capability_ref=cap_ref,
            )
        s.commit()

    print()
    print("Done. Restart the API to refresh cached CapabilityIndex state.")
    print()
    print("Login as AARYA ops user:")
    print(f"  {_OPS_EMAIL} / {_OPS_PASSWORD}")
    print("Or as AARYA admin:")
    print(f"  {_ADMIN_EMAIL} / {_ADMIN_PASSWORD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
