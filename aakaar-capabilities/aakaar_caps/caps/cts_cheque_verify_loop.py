"""cap.cts_cheque_verify_loop — verify every cheque in a fetched CTS batch.

The DAG has no loops/branches, so the per-cheque loop lives here. For each
instrument shown after Fetch it: reads the recorded account number, opens the
cheque BACK image, OCRs the photo with PP-OCRv5 (cap.ocr_account_number,
unchanged), compares to the truth against the env threshold (cap.value_decision),
then either clicks Accept, or fills the "Reject Remark" box and clicks Reject.
It repeats until the "No record Found" popup appears, clicks OK, and writes one
CSV report of every cheque processed (image_url = the cheque image's real URL,
not the managed-storage name). Reuses the other caps verbatim. Side-effecting.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aakaar_caps.browser.state import get_session
from aakaar_caps.caps import (
    csv_report,
    ocr_account_number,
    screenshot,
    value_decision,
    web_click,
    web_fill_field,
    web_read_field,
)
from aakaar_caps.caps._zkutil import JS_HELPERS
from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec

# late binding so this module never has to import its own SPEC inputs at top
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

logger = logging.getLogger(__name__)
CAP_REF = "cap.cts_cheque_verify_loop"


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str = Field(description="Browser session handle (post-login, post-Fetch).")
    delay_seconds: float = Field(default=5.0, ge=0, le=60, description="Delay between UI steps.")
    max_iterations: int = Field(default=50, ge=1, le=2000, description="Safety cap on cheques processed.")
    reject_remark: str = Field(default="MISMATCH:{extracted}",
                               description="Reject-remark template; supports {extracted} and {truth}. "
                               "Capped to remark_maxlen (the CTS field maxlength is 25).")
    remark_maxlen: int = Field(default=25, ge=1, le=200, description="Max remark chars (CTS field maxlength=25).")
    threshold: float | None = Field(default=None, description="Accept threshold; default from env (see cap.value_decision).")
    report_filename: str = Field(default="cts_cheque_verify.csv", description="CSV report filename.")
    expected_length: int = Field(default=14, description="Expected account-number length for the OCR prior.")
    # Selectors / labels (defaults match the CTS Outward DOM; overridable for tests/other portals).
    cheque_selector: str = Field(default="img.z-image[src*='zkau/view']", description="CSS selector of the cheque photo <img>.")
    back_image: str = Field(default="image_back", description="Image hint for the Back-image button (cap.web_click image=).")
    truth_label: str = Field(default="Account No.", description="Label of the recorded account-number column.")
    remark_label: str = Field(default="Reject Remark", description="Label of the reject-remark field.")
    accept_label: str = Field(default="Accept", description="Accept button text.")
    reject_label: str = Field(default="Reject", description="Reject button text.")
    ok_label: str = Field(default="OK", description="OK button text on the terminal popup.")
    no_record_text: str = Field(default="No record found", description="Popup text that ends the loop.")


class _Outputs(BaseModel):
    report_uri: str = Field(description="Managed-storage URI of the CSV report ('' if no cheques).")
    processed: int = Field(description="Cheques processed.")
    accepted: int = Field(description="Cheques accepted.")
    rejected: int = Field(description="Cheques rejected.")
    stopped_reason: str = Field(description="'no_record_found' or 'max_iterations'.")


SPEC = CapabilitySpec(
    ref=CAP_REF,
    description=(
        "Loop over every cheque in a fetched CTS Outward batch: read the recorded account number, "
        "open the back image, OCR it (PP-OCRv5), compare to the truth vs the env threshold, then "
        "Accept or fill the Reject Remark and Reject. Repeats until the 'No record Found' popup, "
        "clicks OK, and writes one CSV report (image_url = cheque image URL). Runs on the agent."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("browser", "ocr", "cheque", "batch"),
    side_effecting=True,
)

_POPUP_JS = r"""
(() => {
  __HELPERS__
  const want = norm(__TEXT__);
  return Array.from(document.querySelectorAll("*")).some(
    (e) => visible(e) && (e.textContent || "").length < 120 && norm(e.textContent).includes(want));
})()
"""

_SRC_JS = r"""
(() => {
  let e = null;
  try { e = document.querySelector(__SEL__); } catch (x) { e = null; }
  return e ? (e.currentSrc || e.getAttribute("src") || "") : "";
})()
"""


async def _popup_present(sess: Any, text: str) -> bool:
    js = _POPUP_JS.replace("__HELPERS__", JS_HELPERS).replace("__TEXT__", json.dumps(text))
    try:
        return bool(await sess.evaluate(js))
    except Exception:  # noqa: BLE001
        return False


async def _cheque_src(sess: Any, selector: str) -> str:
    js = _SRC_JS.replace("__SEL__", json.dumps(selector))
    try:
        return str(await sess.evaluate(js) or "")
    except Exception:  # noqa: BLE001
        return ""


def _build_remark(template: str, extracted: str, truth: str, maxlen: int) -> str:
    try:
        text = template.format(extracted=(extracted or "NONE"), truth=(truth or "NONE"))
    except (KeyError, IndexError, ValueError):
        text = template
    return text[:maxlen]


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = get_session(ctx.session_state, inputs["session"])
    sid = str(inputs["session"])
    delay = float(inputs.get("delay_seconds", 5.0))
    max_iter = int(inputs.get("max_iterations", 50))
    reject_remark = str(inputs.get("reject_remark", "MISMATCH:{extracted}"))
    remark_maxlen = int(inputs.get("remark_maxlen", 25))
    threshold = inputs.get("threshold")
    cheque_selector = str(inputs.get("cheque_selector", "img.z-image[src*='zkau/view']"))
    back_image = str(inputs.get("back_image", "image_back"))
    truth_label = str(inputs.get("truth_label", "Account No."))
    remark_label = str(inputs.get("remark_label", "Reject Remark"))
    accept_label = str(inputs.get("accept_label", "Accept"))
    reject_label = str(inputs.get("reject_label", "Reject"))
    ok_label = str(inputs.get("ok_label", "OK"))
    no_record_text = str(inputs.get("no_record_text", "No record found"))
    report_filename = str(inputs.get("report_filename", "cts_cheque_verify.csv"))
    expected_length = int(inputs.get("expected_length", 14))

    async def settle() -> None:
        if delay > 0:
            await asyncio.sleep(delay)

    async def click_ok() -> None:
        try:
            await web_click.run(ctx, {"session": sid, "text": ok_label})
        except Exception:  # noqa: BLE001
            logger.warning("cts loop: could not click %r on terminal popup", ok_label)

    rows: list[dict[str, Any]] = []
    accepted = rejected = 0
    stopped = "max_iterations"
    logger.info("cap.cts_cheque_verify_loop start run_id=%s delay=%s max=%d", ctx.run_id, delay, max_iter)

    for i in range(max_iter):
        if await _popup_present(sess, no_record_text):
            await click_ok()
            stopped = "no_record_found"
            break

        truth = (await web_read_field.run(
            ctx, {"session": sid, "label": truth_label, "direction": "below"})).get("value", "")

        await web_click.run(ctx, {"session": sid, "image": back_image})  # open the cheque BACK image
        await settle()

        image_url = await _cheque_src(sess, cheque_selector)            # the REAL image URL (for the CSV)
        shot = await screenshot.run(ctx, {"session": sid, "selector": cheque_selector})
        ocr = await ocr_account_number.run(
            ctx, {"image_uri": shot["image_uri"], "expected_length": expected_length})

        dec_inputs: dict[str, Any] = {
            "extracted": ocr["account_number"], "truth": truth,
            "confidence": ocr["heuristic_confidence"],
            "accept_label": accept_label, "reject_label": reject_label}
        if threshold is not None:
            dec_inputs["threshold"] = float(threshold)
        dec = await value_decision.run(ctx, dec_inputs)

        remark = ""
        if dec["decision"] == "reject":
            # Reject flow: click Reject, type a proper remark, then press Enter to advance.
            remark = _build_remark(reject_remark, ocr["account_number"], truth, remark_maxlen)
            await web_click.run(ctx, {"session": sid, "text": reject_label})
            await settle()
            fill = await web_fill_field.run(ctx, {"session": sid, "label": remark_label, "value": remark})
            if fill.get("filled") and fill.get("selector"):
                await sess.press(str(fill["selector"]), "Enter")   # Enter advances to the next cheque
            else:
                logger.warning("cts loop: reject remark field not found for label %r", remark_label)
            rejected += 1
        else:
            await web_click.run(ctx, {"session": sid, "text": accept_label})
            accepted += 1
        await settle()

        rows.append({
            "index": i, "image_url": image_url, "truth_account": truth,
            "extracted_account": ocr["account_number"],
            "model_confidence": ocr["model_confidence"],
            "heuristic_confidence": ocr["heuristic_confidence"],
            "candidate_count": ocr["candidate_count"],
            "match": dec["match"], "threshold_used": dec["threshold_used"],
            "decision": dec["decision"], "remark": remark,
            "ocr_raw_text": (ocr.get("raw_text") or "")[:200],
        })
        logger.info("cts loop #%d: truth=%s ocr=%s -> %s", i, truth, ocr["account_number"], dec["decision"])

        if await _popup_present(sess, no_record_text):
            await click_ok()
            stopped = "no_record_found"
            break

    report_uri = ""
    if rows:
        report = await csv_report.run(ctx, {"filename": report_filename, "rows": rows})
        report_uri = report.get("uri", "")
    logger.info("cap.cts_cheque_verify_loop done processed=%d accepted=%d rejected=%d stop=%s uri=%s",
                len(rows), accepted, rejected, stopped, report_uri)
    return {"report_uri": report_uri, "processed": len(rows),
            "accepted": accepted, "rejected": rejected, "stopped_reason": stopped}
