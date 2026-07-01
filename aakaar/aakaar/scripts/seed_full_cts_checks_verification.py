"""Seed the CHANAKYA workflow 'Full CTS Checks Verification'.

The COMPLETE CTS Outward cheque-verification flow — the same end-to-end
automation the Sandesh fork exposes through its bespoke CTS Automation page,
but delivered here as an ordinary, PARAMETERIZED workflow row. There is no CTS
tab, no `cts.py` router, and no `cap.cts_uat_*` family: it is composed entirely
from Current's generic capabilities and reached through the normal Workflows /
Runs surface.

Every Selection-Criterion form value is a `${inputs.*}` reference, so once
seeded the workflow runs forever through `POST /workflows/{id}/runs` with just
an `inputs` JSON — no planner, no chat session.

Flow (a strict linear chain — the CTS site is a stateful ZK browser session):

    login  → cap.web_login(account_alias)
    ok     → cap.web_click(text="OK")                    # dismiss disclaimer
    nav    → cap.web_tree_select(path=[..., "Ecall Back Processing"])
    fill×5 → cap.web_select(label, value)                # the Selection Criterion
    shot   → cap.screenshot                              # filled form
    fetch  → cap.web_click(text="Fetch")
    truth  → cap.web_read_field(label="Account No.")     # recorded account no.
    back   → cap.web_click(image="image_back")           # open cheque back image
    grab   → cap.screenshot(selector=...)                # capture the cheque
    ocr    → cap.ocr_account_number                      # PP-OCRv5
    decide → cap.value_decision(extracted, truth)        # Accept / Reject
    click  → cap.web_click(text="${decide.click_text}")
    report → cap.csv_report                              # append a row to the CSV
    logout → cap.web_click(image="logout")
    close  → browser.close_session

Run-time `inputs` (all strings) — the Selection Criterion form parameters:

    {
      "account_alias":     "ctsoutward",
      "processing_date":   "19-JUN-2026",
      "record_type":       "TXN",
      "core_system":       "FLEX",
      "cycle_no":          "06",
      "core_batch_number": "0000000144"
    }

`account_alias` picks which Vault-backed `cap.web_login` grant to authenticate
with; the other five drive the Selection Criterion. Change them per run to
verify a different cycle. The OCR / Accept-Reject / report tail runs
automatically — it references node outputs, not form inputs.

    cd aakaar && .venv/bin/python -m aakaar.scripts.seed_full_cts_checks_verification
"""

from __future__ import annotations

import json
import sys
import uuid
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
_NAME = "Full CTS Checks Verification"
_DESC = (
    "Complete CTS Outward cheque verification: log in, open Ecall Back "
    "Processing, fill the Selection Criterion from run inputs, Fetch, read the "
    "recorded account no., OCR the cheque back image (PP-OCRv5), Accept/Reject "
    "vs the truth, write a CSV report, log out. Re-run forever with a different "
    "`inputs` JSON — no planner."
)
_SES = "${login.session}"
_ART = Path(__file__).resolve().parents[3] / "docs" / "design" / "full-cts-checks-verification-dag.json"

# (id, kind, ref, inputs, wait_before). The six Selection Criterion form values
# are `${inputs.*}` refs so the same DAG verifies every cycle; the OCR / decide /
# report tail references node OUTPUTS and stays fixed. Field LABELS are the real
# on-screen strings (matching the existing CTSChequeVerify seed, ZK typo and all).
_STEPS: list[tuple[str, NodeKind, str, dict[str, object], bool]] = [
    ("login", NodeKind.CAPABILITY, "cap.web_login",
     {"account_alias": "${inputs.account_alias}"}, False),
    ("click_okay", NodeKind.CAPABILITY, "cap.web_click", {"session": _SES, "text": "OK"}, True),
    ("open_ecallback", NodeKind.CAPABILITY, "cap.web_tree_select",
     {"session": _SES, "path": ["E-Callback Processing", "Ecall Back Processing"]}, True),
    ("set_processing_date", NodeKind.CAPABILITY, "cap.web_select",
     {"session": _SES, "label": "Processsing Date", "value": "${inputs.processing_date}"}, True),
    ("set_record_type", NodeKind.CAPABILITY, "cap.web_select",
     {"session": _SES, "label": "Record Type", "value": "${inputs.record_type}"}, True),
    ("set_core_system", NodeKind.CAPABILITY, "cap.web_select",
     {"session": _SES, "label": "Core System", "value": "${inputs.core_system}"}, True),
    ("set_cycle_no", NodeKind.CAPABILITY, "cap.web_select",
     {"session": _SES, "label": "Cycle No", "value": "${inputs.cycle_no}"}, True),
    ("set_core_batch", NodeKind.CAPABILITY, "cap.web_select",
     {"session": _SES, "label": "Core Batch Number", "value": "${inputs.core_batch_number}"}, True),
    ("screenshot_form", NodeKind.CAPABILITY, "cap.screenshot", {"session": _SES}, True),
    ("click_fetch", NodeKind.CAPABILITY, "cap.web_click", {"session": _SES, "text": "Fetch"}, True),
    ("read_truth", NodeKind.CAPABILITY, "cap.web_read_field",
     {"session": _SES, "label": "Account No.", "direction": "below"}, True),
    ("click_back_image", NodeKind.CAPABILITY, "cap.web_click", {"session": _SES, "image": "image_back"}, True),
    ("download_cheque", NodeKind.CAPABILITY, "cap.screenshot",
     {"session": _SES, "selector": "img.z-image[src*='zkau/view']"}, True),
    ("ocr", NodeKind.CAPABILITY, "cap.ocr_account_number",
     {"image_uri": "${download_cheque.image_uri}"}, False),
    ("decide", NodeKind.CAPABILITY, "cap.value_decision",
     {"extracted": "${ocr.account_number}", "truth": "${read_truth.value}",
      "confidence": "${ocr.heuristic_confidence}"}, False),
    ("click_decision", NodeKind.CAPABILITY, "cap.web_click",
     {"session": _SES, "text": "${decide.click_text}"}, True),
    ("report", NodeKind.CAPABILITY, "cap.csv_report",
     {"filename": "full_cts_checks_verification.csv", "row": {
         "image_name": "${download_cheque.image_uri}",
         "truth_account": "${read_truth.value}",
         "extracted_account": "${ocr.account_number}",
         "model_confidence": "${ocr.model_confidence}",
         "heuristic_confidence": "${ocr.heuristic_confidence}",
         "decision": "${decide.decision}",
     }}, False),
    ("logout", NodeKind.CAPABILITY, "cap.web_click", {"session": _SES, "image": "logout"}, True),
    ("close", NodeKind.ACTION, "browser.close_session", {"session": _SES}, False),
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


def _validate(dag: Dag, session: Session, tenant_id: uuid.UUID) -> None:
    reg = build_default_registry()
    acts = ActivityRegistry()
    load_into(reg, acts)
    register_shared(reg, acts)
    granted = grants_repo.list_granted_refs(session, tenant_id)
    validate_dag(dag, registry=reg, granted_capabilities=granted)


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
            .order_by(User.created_at)
        ).first()
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
            dag=dag, rationale="Seeded Full CTS Checks Verification (parameterized via ${inputs.*}).")
        s.commit()
        print(f"created workflow {_NAME!r} id={wf.id} version={ver.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
