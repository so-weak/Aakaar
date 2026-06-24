"""Add CTSChequeVerify v2 — batch loop with reject-remark + 'No record found' stop.

v1 verified a single cheque. v2 replaces the per-cheque nodes with one
cap.cts_cheque_verify_loop node that loops every cheque in the fetched batch:
read truth -> open back image -> OCR (PP-OCRv5) -> compare vs env threshold ->
Accept, or fill Reject Remark + Reject -> repeat until 'No record found' -> OK,
then writes one CSV (image_url = the cheque image URL). The DAG then logs out.

    cd aakaar && .venv/bin/python -m aakaar.scripts.seed_cts_cheque_verify_v2
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
_NAME = "CTSChequeVerify"
_SES = "${login_ctsoutward.session}"
_ART = Path(__file__).resolve().parents[3] / "docs" / "design" / "ctschequeverify-v2-dag.json"

# (id, kind, ref, inputs, wait_before)
_STEPS: list[tuple[str, NodeKind, str, dict, bool]] = [
    ("login_ctsoutward", NodeKind.CAPABILITY, "cap.web_login", {"account_alias": "ctsoutward"}, False),
    ("click_okay", NodeKind.CAPABILITY, "cap.web_click", {"session": _SES, "text": "OK"}, True),
    ("open_ecallback", NodeKind.CAPABILITY, "cap.web_tree_select",
     {"session": _SES, "path": ["E-Callback Processing", "Ecall Back Processing"]}, True),
    ("set_processing_date", NodeKind.CAPABILITY, "cap.web_select",
     {"session": _SES, "label": "Processsing Date", "value": "19-JUN-2026"}, True),
    ("set_record_type", NodeKind.CAPABILITY, "cap.web_select",
     {"session": _SES, "label": "Record Type", "value": "TXN"}, True),
    ("set_core_system", NodeKind.CAPABILITY, "cap.web_select",
     {"session": _SES, "label": "Core System", "value": "FLEX"}, True),
    ("set_cycle_no", NodeKind.CAPABILITY, "cap.web_select",
     {"session": _SES, "label": "Cycle No", "value": "06"}, True),
    ("set_core_batch", NodeKind.CAPABILITY, "cap.web_select",
     {"session": _SES, "label": "Core Batch Number", "value": "0000000144"}, True),
    ("screenshot_form", NodeKind.CAPABILITY, "cap.screenshot", {"session": _SES}, True),
    ("click_fetch", NodeKind.CAPABILITY, "cap.web_click", {"session": _SES, "text": "Fetch"}, True),
    ("verify_loop", NodeKind.CAPABILITY, "cap.cts_cheque_verify_loop",
     {"session": _SES, "delay_seconds": 5, "report_filename": "cts_cheque_verify.csv"}, True),
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
        wf = next((w for w in workflows_repo.list_workflows(s, tenant.id) if w.name == _NAME), None)
        if wf is None or admin is None:
            print(f"ERROR: workflow {_NAME!r} or tenant_admin not found", file=sys.stderr)
            return 1
        _validate(dag, s, tenant.id)
        print(f"DAG valid for {_TENANT_SLUG}: {len(dag.nodes)} nodes, {len(dag.edges)} edges")
        _ART.write_text(json.dumps(dag.model_dump(by_alias=True), indent=2) + "\n")
        print(f"wrote artifact: {_ART}")
        # idempotency: skip if a version already uses the loop cap
        for v in range(1, wf.latest_version + 1):
            ver = workflows_repo.get_version(s, tenant.id, wf.id, v)
            if ver and any(n.get("ref") == "cap.cts_cheque_verify_loop" for n in ver.dag.get("nodes", [])):
                print(f"{_NAME}: v{v} already uses cap.cts_cheque_verify_loop; nothing to do.")
                return 0
        new = workflows_repo.add_version(
            s, tenant_id=tenant.id, workflow_id=wf.id, created_by=admin.id, dag=dag,
            rationale="v2: batch loop (OCR verify every cheque, reject-remark, stop on No record found).")
        s.commit()
        print(f"added {_NAME} version={new.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
