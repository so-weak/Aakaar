"""End-to-end test for cap.cts_back_image_sweep: sweep 2 batches, harvest each
batch's back image, and produce ONE ZIP with per-batch subfolders.

Mock: native <select> criteria (so cap.web_select drives them), a Fetch that shows
the cheque for the selected batch, an Account-No. grid, a cheque <img>, Back-image
and Next-Instrument icons, and a "No record found!" popup after one cheque per
batch. Skips if Chromium is unavailable.
"""

from __future__ import annotations

import base64
import contextlib
import io
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("playwright.async_api")
from aakaar_caps.browser.playwright import PlaywrightBrowserSession  # noqa: E402
from aakaar_caps.browser.state import SessionHolder, stash_key  # noqa: E402
from aakaar_caps.caps import cts_back_image_sweep  # noqa: E402
from aakaar_caps.context import CapabilityContext  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
CH1 = REPO / "docs" / "exampleCheques" / "00132990000025.png"
CH2 = REPO / "docs" / "exampleCheques" / "50200100550851.png"


def _html() -> str:
    if not (CH1.exists() and CH2.exists()):
        pytest.skip("example cheques not present")
    a = "data:image/png;base64," + base64.b64encode(CH1.read_bytes()).decode()
    b = "data:image/png;base64," + base64.b64encode(CH2.read_bytes()).decode()
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
          <span class="z-label" id="truthval"></span></div></td></tr>
      </tbody></table></div>

      <img id="cheque" class="z-image" src="" style="width:900px;display:none">
      <img id="bbtn" class="z-image" title="Back Image (Alt+F1)" src="/outward/images/image_back.png"
           style="width:20px;height:20px;cursor:pointer">
      <img id="next" class="z-image" title="Next Instrument (F10)" src="/outward/images/image_skip.png"
           style="width:20px;height:20px;cursor:pointer" onclick="nextInstrument()">
      <div id="popup" style="display:none"><span class="z-label">No record found!</span>
        <button type="button" class="z-button"
          onclick="document.getElementById('popup').style.display='none'">OK</button></div>
      <script>
        window.__cheques = {{
          "0000000144": {{src:"{a}", acct:"00132990000025"}},
          "0000000155": {{src:"{b}", acct:"50200100550851"}}}};
        window.__s = {{idx:0, N:1}};
        function fetchBatch() {{
          var c = window.__cheques[document.getElementById('b').value];
          document.getElementById('cheque').src = c.src;
          document.getElementById('truthval').textContent = c.acct;
          document.getElementById('cheque').style.display = 'block';
          window.__s.idx = 0;
          document.getElementById('popup').style.display = 'none';
        }}
        function nextInstrument() {{
          window.__s.idx++;
          if (window.__s.idx >= window.__s.N) document.getElementById('popup').style.display = 'block';
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

    class _NoopCM:
        async def __aexit__(self, *a: object) -> None:  # pragma: no cover
            return None

    state: dict[str, Any] = {stash_key(session.id): SessionHolder(cm=_NoopCM(), session=session)}
    return CapabilityContext(session_state=state, run_id="bisweep", object_writer=writer), store


async def test_back_image_sweep_consolidated_zip() -> None:
    async with _page() as (page, context):
        s = PlaywrightBrowserSession(_id="BIS", page=page, context=context)
        ctx, store = _ctx(s)
        batches = [{"date": "19-JUN-2026",
                    "cycles": [{"cycle": "06", "batches": ["0000000144", "0000000155"]}]}]
        out = await cts_back_image_sweep.run(ctx, {
            "session": s.id, "batches": batches, "delay_seconds": 0, "cheque_selector": "#cheque"})

        assert out["batches_processed"] == 2
        assert out["count"] == 2
        zip_bytes = store[out["zip_uri"]]
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = set(zf.namelist())
            assert names == {
                "19-JUN-2026/06/0000000144/00132990000025.png",
                "19-JUN-2026/06/0000000155/50200100550851.png",
                "manifest.csv",
            }
            assert len(zf.read("19-JUN-2026/06/0000000144/00132990000025.png")) > 0
