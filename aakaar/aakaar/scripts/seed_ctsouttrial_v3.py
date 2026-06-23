"""Save the fixed CTSOutTrial DAG as a new version (v3) for CHANAKYA.

v2 fails at `click_logout` (browser.click_by_text 'logout' — the logout is an
icon image, no text). This persists the fix from
docs/design/ctsouttrial-working-dag.json, which clicks the logout icon via
`cap.web_click(image="logout")`, as the next workflow version.

Validates against CHANAKYA's grants, then appends via the workflows repository.
Idempotent: if a version already uses cap.web_click for click_logout, it skips.

    cd aakaar && .venv/bin/python -m aakaar.scripts.seed_ctsouttrial_v3
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from aakaar.api.repositories import grants as grants_repo
from aakaar.api.repositories import workflows as workflows_repo
from aakaar.capabilities._base import load_into
from aakaar.capabilities._shared import register_shared
from aakaar.core.config import load_settings
from aakaar.db.models import Tenant, User, UserRole
from aakaar.db.session import EngineConfig, SessionFactory, make_engine
from aakaar.interpreter.activities.registry import ActivityRegistry
from aakaar.shared.dag.types import Dag
from aakaar.shared.dag.validator import validate_dag
from aakaar.shared.registry import build_default_registry

_TENANT_SLUG = "chanakya"
_NAME = "CTSOutTrial"
_ARTIFACT = Path(__file__).resolve().parents[3] / "docs" / "design" / "ctsouttrial-working-dag.json"


def _logout_ref(dag: Dag) -> str | None:
    for n in dag.nodes:
        if n.id == "click_logout":
            return n.ref
    return None


def _validate(dag: Dag, session: Session, tenant_id) -> None:
    reg = build_default_registry()
    acts = ActivityRegistry()
    load_into(reg, acts)
    register_shared(reg, acts)
    granted = grants_repo.list_granted_refs(session, tenant_id)
    validate_dag(dag, registry=reg, granted_capabilities=granted)


def main() -> int:
    settings = load_settings()
    factory = SessionFactory(make_engine(EngineConfig(url=settings.db_url)))
    dag = Dag.model_validate(json.loads(_ARTIFACT.read_text()))
    if _logout_ref(dag) != "cap.web_click":
        print(f"ERROR: artifact's click_logout is {_logout_ref(dag)!r}, expected cap.web_click",
              file=sys.stderr)
        return 1

    with factory.session() as s:
        tenant = s.scalars(select(Tenant).where(Tenant.slug == _TENANT_SLUG)).first()
        if tenant is None:
            print(f"ERROR: tenant {_TENANT_SLUG!r} not found", file=sys.stderr)
            return 1
        admin = s.scalars(
            select(User).where(User.tenant_id == tenant.id, User.role == UserRole.TENANT_ADMIN)
            .order_by(User.created_at)
        ).first()
        if admin is None:
            print(f"ERROR: no tenant_admin for {_TENANT_SLUG!r}", file=sys.stderr)
            return 1
        wf = next((w for w in workflows_repo.list_workflows(s, tenant.id) if w.name == _NAME), None)
        if wf is None:
            print(f"ERROR: workflow {_NAME!r} not found", file=sys.stderr)
            return 1

        # Idempotency: skip if a version already carries the cap.web_click fix.
        for v in range(1, wf.latest_version + 1):
            ver = workflows_repo.get_version(s, tenant.id, wf.id, v)
            if ver is not None and _logout_ref(Dag.model_validate(ver.dag)) == "cap.web_click":
                print(f"{_NAME}: v{v} already has the cap.web_click fix; nothing to do.")
                return 0

        _validate(dag, s, tenant.id)
        new = workflows_repo.add_version(
            s, tenant_id=tenant.id, workflow_id=wf.id, created_by=admin.id, dag=dag,
            rationale="Fix logout: click the icon via cap.web_click(image='logout').",
        )
        s.commit()
        print(f"created {_NAME} version={new.version} (logout via cap.web_click)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
