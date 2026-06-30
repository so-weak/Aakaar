"""cap.cts_back_image_sweep — harvest back images across many CTS batches -> one ZIP.

Takes a JSON of dates -> cycles -> batch numbers and, for each (date, cycle,
batch): selects the Selection Criterion, clicks Fetch, and harvests every cheque's
back image (cap.cts_back_image_harvest, reused — read account, open back, download,
Next Instrument). All images go into ONE consolidated ZIP, organised in per-batch
subfolders (``<date>/<cycle>/<batch>/<account>.<ext>``), plus a manifest.csv. The
DAG has no loops, so the date/cycle/batch sweep lives here. Runs on the agent.

`batches` shape is the same as cap.cts_batch_sweep.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar_caps.browser.state import get_session
from aakaar_caps.caps import cts_back_image_harvest, web_click, web_select
from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.cts_back_image_sweep"


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
    delay_seconds: float = Field(default=0.0, ge=0, le=60, description="Delay between UI steps.")
    zip_filename: str = Field(default="back_images_consolidated.zip", description="Consolidated ZIP name.")
    # field labels / fetch (defaults match CTS DOM)
    processing_date_label: str = Field(default="Processsing Date")
    record_type_label: str = Field(default="Record Type")
    core_system_label: str = Field(default="Core System")
    cycle_label: str = Field(default="Cycle No")
    batch_label: str = Field(default="Core Batch Number")
    fetch_label: str = Field(default="Fetch")
    # harvest passthrough (defaults match CTS DOM; overridable for tests/other portals)
    truth_label: str = Field(default="Account No.")
    back_image: str = Field(default="image_back")
    next_image: str = Field(default="image_skip")
    cheque_selector: str = Field(default="img.z-image[src*='zkau/view']")
    no_record_text: str = Field(default="No record found")


class _Outputs(BaseModel):
    zip_uri: str = Field(description="Managed-storage URI of the consolidated ZIP ('' if nothing harvested).")
    batches_processed: int = Field(description="Number of (date,cycle,batch) combinations swept.")
    count: int = Field(description="Total back images harvested across all batches.")


SPEC = CapabilitySpec(
    ref=CAP_REF,
    description=(
        "Sweep many CTS Outward batches from a dates->cycles->batches JSON and harvest every cheque's "
        "back image (reusing cap.cts_back_image_harvest) into ONE consolidated ZIP organised in "
        "per-batch subfolders (date/cycle/batch/account.ext) with a manifest.csv. Runs on the agent."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("browser", "cheque", "download", "zip", "sweep"),
    side_effecting=True,
)


def _s(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(name)) or "x"


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    _ = get_session(ctx.session_state, inputs["session"])  # fail fast if the session is gone
    sid = str(inputs["session"])
    batches = inputs.get("batches") or []
    record_type = str(inputs.get("record_type", "TXN"))
    core_system = str(inputs.get("core_system", "FLEX"))
    delay = float(inputs.get("delay_seconds", 0.0))
    zip_filename = str(inputs.get("zip_filename", "back_images_consolidated.zip"))
    pd_label = str(inputs.get("processing_date_label", "Processsing Date"))
    rt_label = str(inputs.get("record_type_label", "Record Type"))
    cs_label = str(inputs.get("core_system_label", "Core System"))
    cyc_label = str(inputs.get("cycle_label", "Cycle No"))
    batch_label = str(inputs.get("batch_label", "Core Batch Number"))
    fetch_label = str(inputs.get("fetch_label", "Fetch"))

    harvest_base: dict[str, Any] = {
        "session": sid, "write_zip": False, "delay_seconds": delay,
        "truth_label": str(inputs.get("truth_label", "Account No.")),
        "back_image": str(inputs.get("back_image", "image_back")),
        "next_image": str(inputs.get("next_image", "image_skip")),
        "cheque_selector": str(inputs.get("cheque_selector", "img.z-image[src*='zkau/view']")),
        "no_record_text": str(inputs.get("no_record_text", "No record found")),
    }

    async def sel(label: str, value: str) -> None:
        await web_select.run(ctx, {"session": sid, "label": label, "value": value})

    entries: list[tuple[str, bytes]] = []          # (arcname, bytes)
    manifest: list[str] = ["date,cycle,batch,filename,account,image_url"]
    batches_processed = 0

    for d in batches:
        date = str(d["date"])
        for cyc in d.get("cycles", []):
            cycle = str(cyc["cycle"])
            for raw_batch in cyc.get("batches", []):
                batch = str(raw_batch)
                logger.info("cts back-image sweep: date=%s cycle=%s batch=%s", date, cycle, batch)
                await sel(pd_label, date)
                await sel(rt_label, record_type)
                await sel(cs_label, core_system)
                await sel(cyc_label, cycle)
                await sel(batch_label, batch)
                await web_click.run(ctx, {"session": sid, "text": fetch_label})

                res = await cts_back_image_harvest.run(ctx, dict(harvest_base))
                folder = f"{_s(date)}/{_s(cycle)}/{_s(batch)}"
                for fname, data, account, src in res.get("images_raw", []):
                    entries.append((f"{folder}/{fname}", data))
                    manifest.append(f"{date},{cycle},{batch},{fname},{account},{src}")
                batches_processed += 1

    zip_uri = ""
    if entries:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for arcname, data in entries:
                zf.writestr(arcname, data)
            zf.writestr("manifest.csv", "\n".join(manifest))
        zip_uri = await ctx.write_object(f"runs/{ctx.run_id}/back_images/{zip_filename}", buf.getvalue())

    logger.info("cap.cts_back_image_sweep done batches=%d images=%d uri=%s",
                batches_processed, len(entries), zip_uri)
    return {"zip_uri": zip_uri, "batches_processed": batches_processed, "count": len(entries)}
