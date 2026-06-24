"""Tests for the CTS cheque-verify capabilities:
  - cap.web_read_field   (real Chromium; value-below-header grid + beside + input)
  - cap.value_decision   (accept/reject vs truth + threshold, incl. env threshold)
  - cap.csv_report       (CSV written to a fake object store)
  - cap.ocr_account_number (real PP-OCRv5 on an example cheque)
Each engine/browser test skips cleanly if its dependency is missing.
"""

from __future__ import annotations

import contextlib
import csv
import io
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from aakaar_caps.caps import csv_report, value_decision
from aakaar_caps.context import CapabilityContext

REPO = Path(__file__).resolve().parents[2]
CHEQUES = REPO / "docs" / "exampleCheques"


# ----------------------------- cap.value_decision -----------------------------
async def test_decision_accept_on_match_and_conf() -> None:
    out = await value_decision.run(CapabilityContext(), {
        "extracted": "50200100550851", "truth": "50200100550851",
        "confidence": 0.8, "threshold": 0.6})
    assert out["decision"] == "accept" and out["click_text"] == "Accept"
    assert out["match"] is True and out["similarity"] == 1.0


async def test_decision_reject_on_mismatch() -> None:
    out = await value_decision.run(CapabilityContext(), {
        "extracted": "00000000000000", "truth": "50200100550851",
        "confidence": 0.99, "threshold": 0.6})
    assert out["decision"] == "reject" and out["click_text"] == "Reject" and out["match"] is False


async def test_decision_reject_on_low_confidence() -> None:
    out = await value_decision.run(CapabilityContext(), {
        "extracted": "50200100550851", "truth": "50200100550851",
        "confidence": 0.50, "threshold": 0.60})
    assert out["decision"] == "reject" and out["match"] is True  # matched but below threshold


async def test_decision_threshold_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AAKAAR_OCR_ACCEPT_THRESHOLD", "0.90")
    out = await value_decision.run(CapabilityContext(), {
        "extracted": "50200100550851", "truth": "50200100550851", "confidence": 0.85})
    assert out["threshold_used"] == 0.9 and out["decision"] == "reject"  # 0.85 < 0.90 from env


async def test_decision_digits_only_strips_labels() -> None:
    out = await value_decision.run(CapabilityContext(), {
        "extracted": "A/c No 50200100550851", "truth": "50200100550851",
        "confidence": 0.9, "threshold": 0.6})
    assert out["match"] is True and out["decision"] == "accept"


# ------------------------------- cap.csv_report -------------------------------
async def test_csv_report_writes_row() -> None:
    captured: dict[str, bytes] = {}

    async def _writer(key: str, data: bytes) -> str:
        captured[key] = data
        return "aakaar://t/x/" + key

    ctx = CapabilityContext(run_id="r1", object_writer=_writer)
    row = {"image_name": "chq.png", "truth_account": "50200100550851",
           "extracted_account": "50200100550851", "model_confidence": 0.98,
           "heuristic_confidence": 0.81, "decision": "accept"}
    out = await csv_report.run(ctx, {"filename": "cts.csv", "row": row})
    assert out["uri"].startswith("aakaar://") and out["rows_written"] == 1

    data = next(iter(captured.values())).decode()
    parsed = list(csv.DictReader(io.StringIO(data)))
    assert len(parsed) == 1
    assert parsed[0]["truth_account"] == "50200100550851"
    assert parsed[0]["decision"] == "accept"
    assert parsed[0]["extracted_account"] == "50200100550851"


# ----------------------------- cap.web_read_field -----------------------------
pytest.importorskip("playwright.async_api")
from aakaar_caps.browser.playwright import PlaywrightBrowserSession  # noqa: E402
from aakaar_caps.browser.state import SessionHolder, stash_key  # noqa: E402
from aakaar_caps.caps import web_read_field  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

# CTS instrument grid: header labels row, then values row (account no. BELOW the header).
_GRID_HTML = """<!doctype html><html><body>
  <div class="z-grid"><table><tbody class="z-rows">
    <tr class="z-row">
      <td class="z-row-inner"><div class="z-row-content"><span class="z-label">Account No.</span></div></td>
      <td class="z-row-inner"><div class="z-row-content"><span class="z-label">Cheque No.</span></div></td>
      <td class="z-row-inner"><div class="z-row-content"><span class="z-label">Amount</span></div></td>
    </tr>
    <tr class="z-row z-grid-odd">
      <td class="z-row-inner"><div class="z-row-content"><span class="z-label">50200100550851</span></div></td>
      <td class="z-row-inner"><div class="z-row-content"><span class="z-label">454360</span></div></td>
      <td class="z-row-inner"><div class="z-row-content"><span class="z-label">25,000.00</span></div></td>
    </tr>
  </tbody></table></div>
  <table><tr>
    <td><span class="z-label">IFSC :</span></td><td><span class="z-label">HDFC0000001</span></td>
  </tr></table>
  <label for="acc2">Acct</label><input id="acc2" value="999111222333">
</body></html>"""


@contextlib.asynccontextmanager
async def _page(html: str) -> AsyncIterator[tuple[Any, Any]]:
    pw = await async_playwright().start()
    browser = None
    try:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium not available: {e}")
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.set_content(html, wait_until="domcontentloaded")
        yield page, ctx
    finally:
        if browser is not None:
            await browser.close()
        await pw.stop()


def _ctx_for(session: PlaywrightBrowserSession) -> CapabilityContext:
    class _NoopCM:
        async def __aexit__(self, *a: object) -> None:  # pragma: no cover
            return None
    state: dict[str, Any] = {stash_key(session.id): SessionHolder(cm=_NoopCM(), session=session)}
    return CapabilityContext(session_state=state, run_id="r")


async def test_read_field_value_below_header() -> None:
    async with _page(_GRID_HTML) as (page, context):
        s = PlaywrightBrowserSession(_id="g1", page=page, context=context)
        out = await web_read_field.run(_ctx_for(s), {"session": s.id, "label": "Account No.", "direction": "below"})
        assert out["value"] == "50200100550851" and out["via"] == "below"


async def test_read_field_auto_picks_below() -> None:
    async with _page(_GRID_HTML) as (page, context):
        s = PlaywrightBrowserSession(_id="g2", page=page, context=context)
        out = await web_read_field.run(_ctx_for(s), {"session": s.id, "label": "Account No."})
        assert out["value"] == "50200100550851"


async def test_read_field_beside_label() -> None:
    async with _page(_GRID_HTML) as (page, context):
        s = PlaywrightBrowserSession(_id="g3", page=page, context=context)
        out = await web_read_field.run(_ctx_for(s), {"session": s.id, "label": "IFSC :", "direction": "beside"})
        assert out["value"] == "HDFC0000001" and out["via"] == "beside"


async def test_read_field_input_value() -> None:
    async with _page(_GRID_HTML) as (page, context):
        s = PlaywrightBrowserSession(_id="g4", page=page, context=context)
        out = await web_read_field.run(_ctx_for(s), {"session": s.id, "label": "Acct", "direction": "input"})
        assert out["value"] == "999111222333" and out["via"] == "input"


# --------------------------- cap.ocr_account_number ---------------------------
async def test_ocr_account_number_reads_cheque() -> None:
    pytest.importorskip("rapidocr")
    img = CHEQUES / "00132990000025.png"
    if not img.exists():
        pytest.skip("example cheque not present")
    from aakaar_caps.caps import ocr_account_number

    async def _reader(uri: str) -> bytes:
        return img.read_bytes()

    ctx = CapabilityContext(run_id="ocr1", object_reader=_reader)
    out = await ocr_account_number.run(ctx, {"image_uri": "aakaar://t/x/chq.png"})
    # PP-OCRv5 reads this neatly-written number exactly (verified ~0.99).
    assert out["account_number"] == "00132990000025"
    assert 0.0 < out["model_confidence"] <= 1.0
    assert 0.0 < out["heuristic_confidence"] <= 1.0
    assert out["candidate_count"] >= 1
