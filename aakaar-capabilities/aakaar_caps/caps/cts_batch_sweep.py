"""cap.cts_batch_sweep — verify many CTS batches and write ONE consolidated report.

Takes a JSON of dates -> cycles -> batch numbers and, for each (date, cycle,
batch): selects the Selection Criterion (Processing Date, Record Type, Core
System, Cycle No, Core Batch Number), clicks Fetch, runs the per-cheque verify
loop (cap.cts_cheque_verify_loop, reused verbatim — OCR + accept/reject + remark),
and tags every cheque row with its batch details. After all batches it writes a
single CSV report with the batch columns first. The DAG has no loops, so the
date/cycle/batch sweep lives here. Runs on the agent. Side-effecting.

`batches` shape::

    [
      {"date": "19-JUN-2026", "cycles": [
          {"cycle": "06", "batches": ["0000000144", "0000000155"]},
          {"cycle": "07", "batches": ["0000000160"]}
      ]},
      {"date": "18-JUN-2026", "cycles": [ ... ]}
    ]
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar_caps.browser.state import get_session
from aakaar_caps.caps import csv_report, cts_cheque_verify_loop, web_click, web_select
from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.cts_batch_sweep"

_REPORT_COLUMNS = [
    "batch_date", "batch_cycle", "batch_number",
    "index", "image_url", "truth_account", "extracted_account",
    "model_confidence", "heuristic_confidence", "candidate_count",
    "match", "similarity", "threshold_used", "decision", "remark", "ocr_raw_text",
]


class _CycleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cycle: str = Field(description="Cycle No value to select.")
    batches: list[str] = Field(description="Core Batch Number values under this cycle.")


class _DateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: str = Field(description="Processing Date value to select.")
    cycles: list[_CycleSpec] = Field(description="Cycles (each with its batch numbers) for this date.")


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str = Field(description="Browser session handle (post-login, on the Ecall Back Processing screen).")
    batches: list[_DateSpec] = Field(description="Dates -> cycles -> batch numbers to sweep.")
    record_type: str = Field(default="TXN", description="Record Type to select for every batch.")
    core_system: str = Field(default="FLEX", description="Core System to select for every batch.")
    threshold: float | None = Field(default=None, description="Accept threshold (else env / default).")
    delay_seconds: float = Field(default=5.0, ge=0, le=60, description="Delay between UI steps.")
    expected_length: int = Field(default=14, description="Expected account-number length (OCR prior).")
    reject_remark: str = Field(default="aakaar acc no mismatch", description="Reject remark text.")
    report_filename: str = Field(default="cts_cheque_verify_consolidated.csv", description="Consolidated CSV name.")
    # field labels / fetch (defaults match CTS DOM)
    processing_date_label: str = Field(default="Processsing Date")
    record_type_label: str = Field(default="Record Type")
    core_system_label: str = Field(default="Core System")
    cycle_label: str = Field(default="Cycle No")
    batch_label: str = Field(default="Core Batch Number")
    fetch_label: str = Field(default="Fetch")
    # verify-loop passthrough (defaults match CTS DOM; overridable for tests/other portals)
    cheque_selector: str = Field(default="img.z-image[src*='zkau/view']")
    no_record_text: str = Field(default="No record found")


class _Outputs(BaseModel):
    report_uri: str = Field(description="Managed-storage URI of the consolidated CSV ('' if nothing processed).")
    batches_processed: int = Field(description="Number of (date,cycle,batch) combinations swept.")
    processed: int = Field(description="Total cheques processed across all batches.")
    accepted: int = Field(description="Total cheques accepted.")
    rejected: int = Field(description="Total cheques rejected.")


SPEC = CapabilitySpec(
    ref=CAP_REF,
    description=(
        "Sweep many CTS Outward batches from a dates->cycles->batches JSON: for each batch, select "
        "the Selection Criterion, Fetch, OCR-verify every cheque (reusing cap.cts_cheque_verify_loop), "
        "tag rows with batch details, and write ONE consolidated CSV. Runs on the agent."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("browser", "ocr", "cheque", "batch", "sweep"),
    side_effecting=True,
)


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    _ = get_session(ctx.session_state, inputs["session"])  # fail fast if the session is gone
    sid = str(inputs["session"])
    batches = inputs.get("batches") or []
    record_type = str(inputs.get("record_type", "TXN"))
    core_system = str(inputs.get("core_system", "FLEX"))
    threshold = inputs.get("threshold")
    delay = float(inputs.get("delay_seconds", 5.0))
    expected_length = int(inputs.get("expected_length", 14))
    reject_remark = str(inputs.get("reject_remark", "aakaar acc no mismatch"))
    report_filename = str(inputs.get("report_filename", "cts_cheque_verify_consolidated.csv"))
    pd_label = str(inputs.get("processing_date_label", "Processsing Date"))
    rt_label = str(inputs.get("record_type_label", "Record Type"))
    cs_label = str(inputs.get("core_system_label", "Core System"))
    cyc_label = str(inputs.get("cycle_label", "Cycle No"))
    batch_label = str(inputs.get("batch_label", "Core Batch Number"))
    fetch_label = str(inputs.get("fetch_label", "Fetch"))
    cheque_selector = str(inputs.get("cheque_selector", "img.z-image[src*='zkau/view']"))
    no_record_text = str(inputs.get("no_record_text", "No record found"))

    async def sel(label: str, value: str) -> None:
        await web_select.run(ctx, {"session": sid, "label": label, "value": value})

    loop_base: dict[str, Any] = {
        "session": sid, "write_report": False, "delay_seconds": delay,
        "expected_length": expected_length, "reject_remark": reject_remark,
        "cheque_selector": cheque_selector, "no_record_text": no_record_text,
    }
    if threshold is not None:
        loop_base["threshold"] = float(threshold)

    consolidated: list[dict[str, Any]] = []
    batches_processed = accepted = rejected = 0

    for d in batches:
        date = str(d["date"])
        for cyc in d.get("cycles", []):
            cycle = str(cyc["cycle"])
            for raw_batch in cyc.get("batches", []):
                batch = str(raw_batch)
                logger.info("cts sweep: date=%s cycle=%s batch=%s", date, cycle, batch)
                # Selection Criterion (Core Batch Number last — it depends on date+cycle).
                await sel(pd_label, date)
                await sel(rt_label, record_type)
                await sel(cs_label, core_system)
                await sel(cyc_label, cycle)
                await sel(batch_label, batch)
                await web_click.run(ctx, {"session": sid, "text": fetch_label})

                res = await cts_cheque_verify_loop.run(ctx, dict(loop_base))
                for row in res.get("rows", []):
                    consolidated.append({"batch_date": date, "batch_cycle": cycle,
                                         "batch_number": batch, **row})
                accepted += int(res.get("accepted", 0))
                rejected += int(res.get("rejected", 0))
                batches_processed += 1

    report_uri = ""
    if consolidated:
        report = await csv_report.run(
            ctx, {"filename": report_filename, "rows": consolidated, "columns": _REPORT_COLUMNS})
        report_uri = report.get("uri", "")
    logger.info("cap.cts_batch_sweep done batches=%d cheques=%d accepted=%d rejected=%d uri=%s",
                batches_processed, len(consolidated), accepted, rejected, report_uri)
    return {"report_uri": report_uri, "batches_processed": batches_processed,
            "processed": len(consolidated), "accepted": accepted, "rejected": rejected}
