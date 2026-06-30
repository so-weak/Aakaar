"""Create the CHANAKYA workflow 'CTSChequeVerifyBatch' (multi-batch sweep).

Based on CTSChequeVerify, but instead of one fetched batch it sweeps a JSON of
dates -> cycles -> batch numbers via cap.cts_batch_sweep: for each (date, cycle,
batch) it selects the Selection Criterion, Fetches, OCR-verifies every cheque
(Accept / Reject+remark), and writes ONE consolidated CSV with batch details on
every row. Edit the `batches` input on the sweep node for your real run.

    cd aakaar && .venv/bin/python -m aakaar.scripts.seed_cts_cheque_verify_batch_workflow
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
_DESC = ("CTS Outward: open Ecall Back Processing, then sweep a dates/cycles/batches JSON — per batch "
         "select criteria, Fetch, OCR-verify every cheque (Accept/Reject+remark), consolidated CSV; logout.")
_SES = "${login_ctsoutward.session}"
_ART = Path(__file__).resolve().parents[3] / "docs" / "design" / "ctschequeverifybatch-dag.json"

# Sample sweep — EDIT for the real run (dates -> cycles -> batch numbers).
_BATCHES = [
    {"date": "19-JUN-2026", "cycles": [
        {"cycle": "06", "batches": ["0000000144", "0000000155"]},
    ]},
]

# (id, kind, ref, inputs, wait_before)
_STEPS: list[tuple[str, NodeKind, str, dict, bool]] = [
    ("login_ctsoutward", NodeKind.CAPABILITY, "cap.web_login", {"account_alias": "ctsoutward"}, False),
    ("click_okay", NodeKind.CAPABILITY, "cap.web_click", {"session": _SES, "text": "OK"}, True),
    ("open_ecallback", NodeKind.CAPABILITY, "cap.web_tree_select",
     {"session": _SES, "path": ["E-Callback Processing", "Ecall Back Processing"]}, True),
    ("batch_sweep", NodeKind.CAPABILITY, "cap.cts_batch_sweep",
     {"session": _SES, "batches": _BATCHES, "record_type": "TXN", "core_system": "FLEX",
      "delay_seconds": 5, "report_filename": "cts_cheque_verify_consolidated.csv"}, True),
    ("logout", NodeKind.CAPABILITY, "cap.web_click", {"session": _SES, "image": "logout"}, True),
    ("close_ctsoutward", NodeKind.ACTION, "browser.close_session", {"session": _SES}, False),
]


def _build_dag() -> Dag:
    nodes: list[Node] = []
    order: list[str] = []
    for nid, kind, ref, inputs, wait_before in _STEPS:
        if wait_before:
            wid = f"wait_before_{nid}"
            nodes.append(Node(id=wid, kind=NodeKind.CONTROL, ref="control.wait", inputs={"seconds": 5}))
            order.append(wid)
        nodes.append(Node(id=nid, kind=kind, ref=ref, inputs=inputs))
        order.append(nid)
    edges = [Edge.model_validate({"from": a, "to": b}) for a, b in zip(order, order[1:], strict=False)]
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
        if tenant is None:
            print(f"ERROR: tenant {_TENANT_SLUG!r} not found", file=sys.stderr)
            return 1
        admin = s.scalars(
            select(User).where(User.tenant_id == tenant.id, User.role == UserRole.TENANT_ADMIN)
            .order_by(User.created_at)).first()
        if admin is None:
            print(f"ERROR: no tenant_admin for {_TENANT_SLUG!r}", file=sys.stderr)
            return 1
        _validate(dag, s, tenant.id)
        print(f"DAG valid for {_TENANT_SLUG}: {len(dag.nodes)} nodes, {len(dag.edges)} edges")
        _ART.write_text(json.dumps(dag.model_dump(by_alias=True), indent=2) + "\n")
        print(f"wrote artifact: {_ART}")
        existing = next((w for w in workflows_repo.list_workflows(s, tenant.id) if w.name == _NAME), None)
        if existing is not None:
            print(f"workflow {_NAME!r} already exists (id={existing.id}); not duplicating.")
            return 0
        wf, ver = workflows_repo.create_workflow(
            s, tenant_id=tenant.id, created_by=admin.id, name=_NAME, description=_DESC,
            dag=dag, rationale="Seeded CTSChequeVerifyBatch (multi-batch sweep -> consolidated report).")
        s.commit()
        print(f"created workflow {_NAME!r} id={wf.id} version={ver.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
