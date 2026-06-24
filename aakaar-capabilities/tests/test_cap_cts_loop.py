"""End-to-end test for cap.cts_cheque_verify_loop + cap.web_fill_field on a mock
CTS instrument page driven through REAL PP-OCRv5.

The mock shows a real example cheque (as a data URL), an Account-No. grid (truth
below the header), a Reject Remark box, Accept/Reject buttons, a Back-image icon,
and a "No record found!" popup that appears after one decision. The loop cap must
read the truth, open the back image, OCR the cheque, decide, click Accept (match)
or fill the remark + Reject (mismatch), then OK the popup and write a CSV whose
image_url is the cheque's real src URL.

Skips if Chromium or rapidocr is unavailable.
"""

from __future__ import annotations

import base64
import contextlib
import csv as _csv
import io
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("playwright.async_api")
pytest.importorskip("rapidocr")
from aakaar_caps.browser.playwright import PlaywrightBrowserSession  # noqa: E402
from aakaar_caps.browser.state import SessionHolder, stash_key  # noqa: E402
from aakaar_caps.caps import cts_cheque_verify_loop, web_fill_field  # noqa: E402
from aakaar_caps.context import CapabilityContext  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
CHEQUE = REPO / "docs" / "exampleCheques" / "00132990000025.png"  # truth = 00132990000025


def _page_html(truth: str) -> str:
    if not CHEQUE.exists():
        pytest.skip("example cheque not present")
    b64 = base64.b64encode(CHEQUE.read_bytes()).decode()
    data_url = f"data:image/png;base64,{b64}"
    return f"""<!doctype html><html><body>
      <div class="z-grid"><table><tbody class="z-rows">
        <tr class="z-row"><td class="z-row-inner"><div class="z-row-content">
          <span class="z-label">Account No.</span></div></td></tr>
        <tr class="z-row z-grid-odd"><td class="z-row-inner"><div class="z-row-content">
          <span class="z-label">{truth}</span></div></td></tr>
      </tbody></table></div>
      <img id="cheque" class="z-image" src="{data_url}" style="width:1000px;display:block">
      <img id="bbtn" class="z-image" title="Back Image (Alt+F1)" src="/outward/images/image_back.png"
           style="width:20px;height:20px;cursor:pointer">
      <table><tr>
        <td><span class="z-label">Reject Remark  : </span></td>
        <td><input id="remark" class="z-textbox" type="text" maxlength="25" value=""></td>
      </tr></table>
      <button type="button" id="acc" class="z-button" onclick="decide('accept')">Accept</button>
      <button type="button" id="rej" class="z-button" onclick="decide('reject')">Reject</button>
      <div id="popup" style="display:none"><span class="z-label">No record found!</span>
        <button type="button" class="z-button"
          onclick="document.getElementById('popup').style.display='none';window.__s.done=true">OK</button></div>
      <script>
        window.__s = {{accepts:0, rejects:0, remark:"", done:false, idx:0, N:1}};
        function decide(kind) {{
          if (kind === 'accept') window.__s.accepts++;
          else {{ window.__s.rejects++; window.__s.remark = document.getElementById('remark').value; }}
          window.__s.idx++;
          if (window.__s.idx >= window.__s.N) document.getElementById('popup').style.display = 'block';
        }}
      </script></body></html>"""


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


def _ctx(session: PlaywrightBrowserSession) -> tuple[CapabilityContext, dict[str, bytes]]:
    store: dict[str, bytes] = {}

    async def writer(key: str, data: bytes) -> str:
        uri = "aakaar://t/x/" + key
        store[uri] = data
        return uri

    async def reader(uri: str) -> bytes:
        return store[uri]

    class _NoopCM:
        async def __aexit__(self, *a: object) -> None:  # pragma: no cover
            return None

    state: dict[str, Any] = {stash_key(session.id): SessionHolder(cm=_NoopCM(), session=session)}
    ctx = CapabilityContext(session_state=state, run_id="loop", object_writer=writer, object_reader=reader)
    return ctx, store


def _csv_rows(store: dict[str, bytes]) -> list[dict[str, str]]:
    _csv.field_size_limit(10_000_000)  # the data-URL image_url cell is large in tests
    csv_uri = next(u for u in store if "/reports/" in u)
    return list(_csv.DictReader(io.StringIO(store[csv_uri].decode())))


async def test_loop_accept_then_no_record() -> None:
    async with _page(_page_html("00132990000025")) as (page, context):
        s = PlaywrightBrowserSession(_id="L1", page=page, context=context)
        ctx, store = _ctx(s)
        out = await cts_cheque_verify_loop.run(ctx, {
            "session": s.id, "delay_seconds": 0, "threshold": 0.0, "cheque_selector": "#cheque"})
        assert out["processed"] == 1 and out["accepted"] == 1 and out["rejected"] == 0
        assert out["stopped_reason"] == "no_record_found"
        assert (await page.evaluate("() => window.__s.accepts")) == 1
        rows = _csv_rows(store)
        assert len(rows) == 1
        r = rows[0]
        assert r["decision"] == "accept"
        assert r["extracted_account"] == "00132990000025"
        assert r["truth_account"] == "00132990000025"
        assert r["match"] == "True"
        assert r["image_url"].startswith("data:image/png")   # the image URL, not the aakaar name


async def test_loop_reject_fills_remark() -> None:
    async with _page(_page_html("99999999999999")) as (page, context):  # truth != OCR -> reject
        s = PlaywrightBrowserSession(_id="L2", page=page, context=context)
        ctx, store = _ctx(s)
        out = await cts_cheque_verify_loop.run(ctx, {
            "session": s.id, "delay_seconds": 0, "threshold": 0.0, "cheque_selector": "#cheque",
            "reject_remark": "ACCT MISMATCH"})
        assert out["processed"] == 1 and out["rejected"] == 1 and out["accepted"] == 0
        assert (await page.evaluate("() => window.__s.remark")) == "ACCT MISMATCH"  # remark typed before Reject
        rows = _csv_rows(store)
        assert rows[0]["decision"] == "reject" and rows[0]["remark"] == "ACCT MISMATCH"
        assert rows[0]["match"] == "False"


async def test_web_fill_field_beside_label() -> None:
    async with _page(_page_html("00132990000025")) as (page, context):
        s = PlaywrightBrowserSession(_id="L3", page=page, context=context)
        ctx, _ = _ctx(s)
        out = await web_fill_field.run(ctx, {"session": s.id, "label": "Reject Remark", "value": "HELLO"})
        assert out["filled"] is True
        assert (await page.eval_on_selector("#remark", "el => el.value")) == "HELLO"
