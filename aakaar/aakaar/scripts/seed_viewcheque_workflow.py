"""Create the CHANAKYA workflow 'ViewChequeCTSOut' (a separate workflow).

Flow (a 5-second wait precedes every step after login):
  login (ctsoutward) -> dismiss OK dialog -> open the menu
  'E-Callback Processing > Ecall Back Processing' -> fill the Selection Criterion
  ZK comboboxes (Processsing Date=19-JUN-2026, Record Type=TXN, Core System=FLEX,
  Cycle No=06, Core Batch Number=0000000144) -> screenshot -> click Fetch ->
  screenshot -> logout (icon) -> close.

Builds the DAG, validates it against CHANAKYA's grants, writes the JSON artifact
to docs/design/viewchequectsout-dag.json, and creates the workflow via the
workflows repository (idempotent by name). Run:

    cd aakaar && .venv/bin/python -m aakaar.scripts.seed_viewcheque_workflow
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
_NAME = "ViewChequeCTSOut"
_DESC = "CTS Outward: open Ecall Back Processing, fill Selection Criterion, Fetch, logout."
_SES = "${login_ctsoutward.session}"
_ARTIFACT = Path(__file__).resolve().parents[3] / "docs" / "design" / "viewchequectsout-dag.json"

# Ordered real steps; a wait(5) is inserted before every step after the login.
_STEPS: list[tuple[str, NodeKind, str, dict]] = [
    ("login_ctsoutward", NodeKind.CAPABILITY, "cap.web_login", {"account_alias": "ctsoutward"}),
    ("click_okay", NodeKind.CAPABILITY, "cap.web_click", {"session": _SES, "text": "OK"}),
    ("open_ecallback", NodeKind.CAPABILITY, "cap.web_tree_select",
     {"session": _SES, "path": ["E-Callback Processing", "Ecall Back Processing"]}),
    ("set_processing_date", NodeKind.CAPABILITY, "cap.web_select",
     {"session": _SES, "label": "Processsing Date", "value": "19-JUN-2026"}),
    ("set_record_type", NodeKind.CAPABILITY, "cap.web_select",
     {"session": _SES, "label": "Record Type", "value": "TXN"}),
    ("set_core_system", NodeKind.CAPABILITY, "cap.web_select",
     {"session": _SES, "label": "Core System", "value": "FLEX"}),
    ("set_cycle_no", NodeKind.CAPABILITY, "cap.web_select",
     {"session": _SES, "label": "Cycle No", "value": "06"}),
    ("set_core_batch", NodeKind.CAPABILITY, "cap.web_select",
     {"session": _SES, "label": "Core Batch Number", "value": "0000000144"}),
    ("screenshot_form", NodeKind.CAPABILITY, "cap.screenshot", {"session": _SES}),
    ("click_fetch", NodeKind.CAPABILITY, "cap.web_click", {"session": _SES, "text": "Fetch"}),
    ("screenshot_results", NodeKind.CAPABILITY, "cap.screenshot", {"session": _SES}),
    ("click_logout", NodeKind.CAPABILITY, "cap.web_click", {"session": _SES, "image": "logout"}),
    ("close_ctsoutward", NodeKind.ACTION, "browser.close_session", {"session": _SES}),
]


def _build_dag() -> Dag:
    nodes: list[Node] = []
    order: list[str] = []
    for i, (nid, kind, ref, inputs) in enumerate(_STEPS):
        if i > 0 and nid != "close_ctsoutward":
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

        _ARTIFACT.write_text(json.dumps(dag.model_dump(by_alias=True), indent=2) + "\n")
        print(f"wrote artifact: {_ARTIFACT}")

        existing = next((w for w in workflows_repo.list_workflows(s, tenant.id) if w.name == _NAME), None)
        if existing is not None:
            print(f"workflow {_NAME!r} already exists (id={existing.id}); not duplicating.")
            return 0
        wf, ver = workflows_repo.create_workflow(
            s, tenant_id=tenant.id, created_by=admin.id, name=_NAME, description=_DESC,
            dag=dag, rationale="Seeded ViewChequeCTSOut (Ecall Back Processing view-cheque flow).",
        )
        s.commit()
        print(f"created workflow {_NAME!r} id={wf.id} version={ver.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
