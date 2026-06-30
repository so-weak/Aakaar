"""Add CTSChequeVerifyBatch v2 — same multi-batch sweep, but with NO waits.

Drops every control.wait node and sets the sweep's internal delay_seconds=0.

    cd aakaar && .venv/bin/python -m aakaar.scripts.seed_cts_cheque_verify_batch_v2
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
from aakaar.shared.dag.types import Dag, Edge, Node, NodeKind
from aakaar.shared.dag.validator import validate_dag
from aakaar.shared.registry import build_default_registry

_TENANT_SLUG = "chanakya"
_NAME = "CTSChequeVerifyBatch"
_SES = "${login_ctsoutward.session}"
_ART = Path(__file__).resolve().parents[3] / "docs" / "design" / "ctschequeverifybatch-v2-dag.json"

_BATCHES = [
    {"date": "19-JUN-2026", "cycles": [
        {"cycle": "06", "batches": ["0000000144", "0000000155"]},
    ]},
]

# No control.wait nodes; sweep delay_seconds=0.
_NODES: list[tuple[str, NodeKind, str, dict]] = [
    ("login_ctsoutward", NodeKind.CAPABILITY, "cap.web_login", {"account_alias": "ctsoutward"}),
    ("click_okay", NodeKind.CAPABILITY, "cap.web_click", {"session": _SES, "text": "OK"}),
    ("open_ecallback", NodeKind.CAPABILITY, "cap.web_tree_select",
     {"session": _SES, "path": ["E-Callback Processing", "Ecall Back Processing"]}),
    ("batch_sweep", NodeKind.CAPABILITY, "cap.cts_batch_sweep",
     {"session": _SES, "batches": _BATCHES, "record_type": "TXN", "core_system": "FLEX",
      "delay_seconds": 0, "report_filename": "cts_cheque_verify_consolidated.csv"}),
    ("logout", NodeKind.CAPABILITY, "cap.web_click", {"session": _SES, "image": "logout"}),
    ("close_ctsoutward", NodeKind.ACTION, "browser.close_session", {"session": _SES}),
]


def _build_dag() -> Dag:
    nodes = [Node(id=nid, kind=kind, ref=ref, inputs=inputs) for nid, kind, ref, inputs in _NODES]
    ids = [n.id for n in nodes]
    edges = [Edge.model_validate({"from": a, "to": b}) for a, b in zip(ids, ids[1:], strict=False)]
    return Dag(nodes=nodes, edges=edges)


def _validate(dag: Dag, session: Session, tenant_id) -> None:
    reg = build_default_registry()
    acts = ActivityRegistry()
    load_into(reg, acts)
    register_shared(reg, acts)
    validate_dag(dag, registry=reg, granted_capabilities=grants_repo.list_granted_refs(session, tenant_id))


def main() -> int:
    settings = load_settings()
    factory = SessionFactory(make_engine(EngineConfig(url=settings.db_url)))
    dag = _build_dag()
    with factory.session() as s:
        tenant = s.scalars(select(Tenant).where(Tenant.slug == _TENANT_SLUG)).first()
        admin = s.scalars(
            select(User).where(User.tenant_id == tenant.id, User.role == UserRole.TENANT_ADMIN)
            .order_by(User.created_at)).first() if tenant else None
        wf = next((w for w in workflows_repo.list_workflows(s, tenant.id) if w.name == _NAME), None) if tenant else None
        if tenant is None or admin is None or wf is None:
            print(f"ERROR: tenant/admin/workflow {_NAME!r} not found", file=sys.stderr)
            return 1
        _validate(dag, s, tenant.id)
        print(f"DAG valid for {_TENANT_SLUG}: {len(dag.nodes)} nodes, {len(dag.edges)} edges (no waits)")
        _ART.write_text(json.dumps(dag.model_dump(by_alias=True), indent=2) + "\n")
        print(f"wrote artifact: {_ART}")
        # idempotency: skip if a version with no control.wait nodes already exists
        for v in range(1, wf.latest_version + 1):
            ver = workflows_repo.get_version(s, tenant.id, wf.id, v)
            if ver and not any(n.get("ref") == "control.wait" for n in ver.dag.get("nodes", [])):
                print(f"{_NAME}: v{v} already has no waits; nothing to do.")
                return 0
        new = workflows_repo.add_version(
            s, tenant_id=tenant.id, workflow_id=wf.id, created_by=admin.id, dag=dag,
            rationale="v2: same multi-batch sweep, no waits (delay_seconds=0, no control.wait nodes).")
        s.commit()
        print(f"added {_NAME} version={new.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
