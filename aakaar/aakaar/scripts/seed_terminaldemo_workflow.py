"""Create the CHANAKYA workflow 'terminalDemo' in the DB so it can be triggered
through the server.

Loads the DAG from examples/11-terminalDemo/workflow.json (two visible+captured
curl terminals on the ashu-mac agent, each step paced by a 5s control.wait),
validates it against CHANAKYA's grants, and creates the workflow via the
workflows repository. Idempotent by name. Run:

    cd aakaar && .venv/bin/python -m aakaar.scripts.seed_terminaldemo_workflow
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlalchemy import select

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
_WF = Path(__file__).resolve().parents[3] / "examples" / "11-terminalDemo" / "workflow.json"


def main() -> int:
    settings = load_settings()
    factory = SessionFactory(make_engine(EngineConfig(url=settings.db_url)))
    body = json.loads(_WF.read_text())
    name = body["name"]
    desc = body.get("description", "")
    dag = Dag.model_validate(body["dag"])

    with factory.session() as s:
        tenant = s.scalars(select(Tenant).where(Tenant.slug == _TENANT_SLUG)).first()
        if tenant is None:
            print(f"ERROR: tenant {_TENANT_SLUG!r} not found", file=sys.stderr)
            return 1
        admin = s.scalars(
            select(User)
            .where(User.tenant_id == tenant.id, User.role == UserRole.TENANT_ADMIN)
            .order_by(User.created_at)
        ).first()
        if admin is None:
            print(f"ERROR: no tenant_admin for {_TENANT_SLUG!r}", file=sys.stderr)
            return 1

        reg = build_default_registry()
        acts = ActivityRegistry()
        load_into(reg, acts)
        register_shared(reg, acts)
        granted = grants_repo.list_granted_refs(s, tenant.id)
        validate_dag(dag, registry=reg, granted_capabilities=granted)
        print(f"DAG valid for {_TENANT_SLUG}: {len(dag.nodes)} nodes, {len(dag.edges)} edges")

        existing = next(
            (w for w in workflows_repo.list_workflows(s, tenant.id) if w.name == name), None
        )
        if existing is not None:
            print(f"workflow {name!r} already exists (id={existing.id}); not duplicating.")
            return 0
        wf, ver = workflows_repo.create_workflow(
            s,
            tenant_id=tenant.id,
            created_by=admin.id,
            name=name,
            description=desc,
            dag=dag,
            rationale="Seeded terminalDemo (two visible+captured curl terminals on ashu-mac).",
        )
        s.commit()
        print(f"created workflow {name!r} id={wf.id} version={ver.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
