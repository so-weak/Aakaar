"""End-to-end test for cap.cts_back_image_harvest: walk a batch via Next
Instrument, download each cheque's back image named by its recorded account
number, and return a ZIP.

Mock CTS screen: an Account-No. grid, a cheque <img> (data URL), a Back-image
icon, a Next-Instrument icon, and a "No record found!" popup after the last
cheque. Next Instrument advances through 2 cheques (two different example images)
then shows the popup. The data-URL download isn't fetchable via request.get, so
the cap falls back to a screenshot — which is what we assert on. Skips if Chromium
isn't available.
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
from aakaar_caps.caps import cts_back_image_harvest  # noqa: E402
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
      <div class="z-grid"><table><tbody class="z-rows">
        <tr class="z-row"><td class="z-row-inner"><div class="z-row-content">
          <span class="z-label">Account No.</span></div></td></tr>
        <tr class="z-row z-grid-odd"><td class="z-row-inner"><div class="z-row-content">
          <span class="z-label" id="truthval"></span></div></td></tr>
      </tbody></table></div>
      <img id="cheque" class="z-image" src="" style="width:900px;display:block">
      <img id="bbtn" class="z-image" title="Back Image (Alt+F1)" src="/outward/images/image_back.png"
           style="width:20px;height:20px;cursor:pointer">
      <img id="next" class="z-image" title="Next Instrument (F10)" src="/outward/images/image_skip.png"
           style="width:20px;height:20px;cursor:pointer" onclick="nextInstrument()">
      <div id="popup" style="display:none"><span class="z-label">No record found!</span>
        <button type="button" class="z-button"
          onclick="document.getElementById('popup').style.display='none'">OK</button></div>
      <script>
        window.__h = {{idx:0, N:2, cheques:[
          {{src:"{a}", acct:"00132990000025"}},
          {{src:"{b}", acct:"50200100550851"}}]}};
        function showCurrent() {{
          var c = window.__h.cheques[window.__h.idx];
          document.getElementById('cheque').src = c.src;
          document.getElementById('truthval').textContent = c.acct;
        }}
        function nextInstrument() {{
          window.__h.idx++;
          if (window.__h.idx < window.__h.N) showCurrent();
          else document.getElementById('popup').style.display = 'block';
        }}
        showCurrent();  // cheque 0 shown on load (post-Fetch state)
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
    ctx = CapabilityContext(session_state=state, run_id="harvest", object_writer=writer)
    return ctx, store


async def test_harvest_back_images_to_zip() -> None:
    async with _page() as (page, context):
        s = PlaywrightBrowserSession(_id="HV", page=page, context=context)
        ctx, store = _ctx(s)
        out = await cts_back_image_harvest.run(ctx, {
            "session": s.id, "delay_seconds": 0, "cheque_selector": "#cheque"})

        assert out["count"] == 2
        assert out["stopped_reason"] == "no_record_found"
        assert out["accounts"] == ["00132990000025", "50200100550851"]
        assert out["zip_uri"].startswith("aakaar://")

        zip_bytes = store[out["zip_uri"]]
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = set(zf.namelist())
            assert names == {"00132990000025.png", "50200100550851.png", "manifest.csv"}
            assert len(zf.read("00132990000025.png")) > 0   # real screenshot bytes
            assert "00132990000025" in zf.read("manifest.csv").decode()
