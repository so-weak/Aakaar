"""End-to-end test for cap.cts_batch_sweep: sweep a dates->cycles->batches JSON,
verify each batch's cheque through real PP-OCRv5, and write ONE consolidated CSV
whose rows carry the batch details.

Mock CTS screen: native <select> Selection Criterion (so cap.web_select drives
them), a Fetch button, an Account-No. grid, a real cheque image, Back-image icon,
Reject Remark box, Accept/Reject, and a "No record found!" popup after one
decision. Fetch (re)shows the cheque and resets state, so each batch processes one
cheque. Skips if Chromium or rapidocr is unavailable.
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
from aakaar_caps.caps import cts_batch_sweep  # noqa: E402
from aakaar_caps.context import CapabilityContext  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
CHEQUE = REPO / "docs" / "exampleCheques" / "00132990000025.png"  # truth 00132990000025


def _html() -> str:
    if not CHEQUE.exists():
        pytest.skip("example cheque not present")
    data_url = "data:image/png;base64," + base64.b64encode(CHEQUE.read_bytes()).decode()
    return f"""<!doctype html><html><body>
      <table><tbody>
        <tr class="z-row"><td><span class="z-label">Processsing Date</span></td>
            <td><select id="d"><option>19-JUN-2026</option></select></td></tr>
        <tr class="z-row"><td><span class="z-label">Record Type</span></td>
            <td><select id="rt"><option>TXN</option></select></td></tr>
        <tr class="z-row"><td><span class="z-label">Core System</span></td>
            <td><select id="cs"><option>FLEX</option></select></td></tr>
        <tr class="z-row"><td><span class="z-label">Cycle No</span></td>
            <td><select id="cy"><option>06</option></select></td></tr>
        <tr class="z-row"><td><span class="z-label">Core Batch Number</span></td>
            <td><select id="b"><option>0000000144</option><option>0000000155</option></select></td></tr>
      </tbody></table>
      <button type="button" id="fetch" class="z-button" onclick="fetchBatch()">Fetch</button>

      <div class="z-grid"><table><tbody class="z-rows">
        <tr class="z-row"><td class="z-row-inner"><div class="z-row-content">
          <span class="z-label">Account No.</span></div></td></tr>
        <tr class="z-row z-grid-odd"><td class="z-row-inner"><div class="z-row-content">
          <span class="z-label">00132990000025</span></div></td></tr>
      </tbody></table></div>

      <img id="cheque" class="z-image" src="{data_url}" style="width:1000px;display:none">
      <img id="bbtn" class="z-image" title="Back Image (Alt+F1)" src="/outward/images/image_back.png"
           style="width:20px;height:20px;cursor:pointer">
      <table><tr><td><span class="z-label">Reject Remark  : </span></td>
        <td><input id="remark" class="z-textbox" type="text" maxlength="25" value=""></td></tr></table>
      <button type="button" id="acc" class="z-button" onclick="acceptClick()">Accept</button>
      <button type="button" id="rej" class="z-button" onclick="rejectClick()">Reject</button>
      <div id="popup" style="display:none"><span class="z-label">No record found!</span>
        <button type="button" class="z-button"
          onclick="document.getElementById('popup').style.display='none'">OK</button></div>
      <script>
        window.__s = {{accepts:0, rejects:0, idx:0, N:1, rejecting:false}};
        window.__sweep = {{fetches:0}};
        function advance() {{ window.__s.idx++; if (window.__s.idx >= window.__s.N)
            document.getElementById('popup').style.display='block'; }}
        function acceptClick() {{ window.__s.accepts++; advance(); }}
        function rejectClick() {{ window.__s.rejecting = true; }}
        document.getElementById('remark').addEventListener('keydown', function (e) {{
          if (e.key === 'Enter' && window.__s.rejecting) {{
            window.__s.rejects++; window.__s.rejecting = false; advance(); }}
        }});
        function fetchBatch() {{
          window.__sweep.fetches++;
          window.__s.idx = 0; window.__s.rejecting = false;
          document.getElementById('popup').style.display = 'none';
          document.getElementById('cheque').style.display = 'block';
        }}
      </script></body></html>"""


@contextlib.asynccontextmanager
async def _page() -> AsyncIterator[tuple[Any, Any]]:
    pw = await async_playwright().start()
    browser = None
    try:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium not available: {e}")
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.set_content(_html(), wait_until="domcontentloaded")
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
    ctx = CapabilityContext(session_state=state, run_id="sweep", object_writer=writer, object_reader=reader)
    return ctx, store


def _csv_rows(store: dict[str, bytes]) -> list[dict[str, str]]:
    _csv.field_size_limit(10_000_000)
    uri = next(u for u in store if "/reports/" in u)
    return list(_csv.DictReader(io.StringIO(store[uri].decode())))


async def test_sweep_two_batches_consolidated_report() -> None:
    async with _page() as (page, context):
        s = PlaywrightBrowserSession(_id="SW", page=page, context=context)
        ctx, store = _ctx(s)
        batches = [{"date": "19-JUN-2026",
                    "cycles": [{"cycle": "06", "batches": ["0000000144", "0000000155"]}]}]
        out = await cts_batch_sweep.run(ctx, {
            "session": s.id, "batches": batches, "delay_seconds": 0, "threshold": 0.0,
            "cheque_selector": "#cheque"})

        assert out["batches_processed"] == 2
        assert out["processed"] == 2 and out["accepted"] == 2
        assert (await page.evaluate("() => window.__sweep.fetches")) == 2

        rows = _csv_rows(store)
        assert len(rows) == 2
        # consolidated report carries batch details on every row
        assert {r["batch_number"] for r in rows} == {"0000000144", "0000000155"}
        for r in rows:
            assert r["batch_date"] == "19-JUN-2026" and r["batch_cycle"] == "06"
            assert r["decision"] == "accept"
            assert r["extracted_account"] == "00132990000025"
        # batch columns come first
        assert list(rows[0].keys())[:3] == ["batch_date", "batch_cycle", "batch_number"]
