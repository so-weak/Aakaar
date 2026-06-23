"""Real-Chromium tests for cap.web_tree_select + cap.web_select against a
faithful CTS Outward ZK tree menu and "Selection Criterion" form.

Reproduces the DOM shapes captured from the live portal:
  * the left menu ZK tree — "E-Callback Processing" (collapsed, with an expand
    caret) and its hidden first child "Ecall Back Processing";
  * ZK comboboxes (readonly input + caret button + popup <li> list) for
    "Processsing Date" (sic) and "Record Type";
  * a native <select> ("Core System") to exercise the select fallback;
  * the "Fetch" <button class="z-button">.

Minimal ZK-like JS mirrors the real client behaviour: the tree caret reveals the
child rows; a combobox button toggles its popup; clicking a popup item sets the
readonly input's value. Skips cleanly when Chromium isn't installed.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytest.importorskip("playwright.async_api")
from aakaar_caps.browser.playwright import PlaywrightBrowserSession  # noqa: E402
from aakaar_caps.browser.state import SessionHolder, stash_key  # noqa: E402
from aakaar_caps.caps import web_select, web_tree_select  # noqa: E402
from aakaar_caps.caps import web_click  # noqa: E402
from aakaar_caps.context import CapabilityContext  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<style>
  /* The real ZK CSS gives these icon/button widgets layout size; reproduce that
     so Playwright's actionable click (the primary path under test) can land. */
  .z-combobox-button { display:inline-block; width:18px; height:18px; }
  .z-combobox-icon { display:inline-block; width:16px; height:16px; }
  .z-tree-icon { display:inline-block; width:18px; height:18px; }
  .z-tree-icon i { display:inline-block; width:16px; height:16px; }
  .z-comboitem { display:block; min-height:16px; }
  .z-button { min-width:60px; min-height:20px; }
  .z-treerow { display:table-row; }
  .z-treerow .z-label { display:inline-block; min-height:14px; }
</style>
</head>
<body class="webkit chrome silvertail">
  <!-- ===== left menu ZK tree ===== -->
  <div class="z-tree" role="treegrid"><table><tbody class="z-treechildren">
    <tr id="r-out" class="z-treerow" aria-expanded="true" aria-level="1" aria-label="Outward">
      <td class="z-treecell"><div class="z-treecell-content">
        <span class="z-tree-icon"><i class="z-icon-caret-down z-tree-open"></i></span>
        <div class="z-label"><span class="z-label">Outward</span></div>
      </div></td>
    </tr>
    <tr id="r-ecb" class="z-treerow" aria-expanded="false" aria-level="2" aria-label="E-Callback Processing">
      <td class="z-treecell"><div class="z-treecell-content">
        <span class="z-tree-icon" id="r-ecb-open" onclick="toggleEcb()">
          <i id="r-ecb-icon" class="z-icon-caret-right z-tree-close"></i></span>
        <div class="z-label"><span class="z-label">E-Callback Processing</span></div>
      </div></td>
    </tr>
    <tr id="r-ebp" class="z-treerow" style="display:none" aria-level="3" aria-label="Ecall Back Processing"
        onclick="window.__fired.ebp = true">
      <td class="z-treecell"><div class="z-treecell-content">
        <span class="z-tree-line z-tree-spacer"></span>
        <div class="z-label"><span class="z-label">Ecall Back Processing</span></div>
      </div></td>
    </tr>
  </tbody></table></div>

  <!-- ===== Selection Criterion form ===== -->
  <div class="z-grid"><table><tbody class="z-rows">
    <tr class="z-row">
      <td class="z-row-inner"><div class="z-row-content"><span class="z-label">Processsing Date</span></div></td>
      <td class="z-row-inner"><div class="z-row-content">
        <span id="cb-pd" class="z-combobox z-combobox-readonly" role="combobox" aria-owns="cb-pd-pp">
          <input id="cb-pd-real" class="z-combobox-input" readonly value="">
          <a id="cb-pd-btn" class="z-combobox-button" role="button" onclick="toggleCb('cb-pd')">
            <i class="z-combobox-icon z-icon-caret-down"></i></a>
          <div id="cb-pd-pp" class="z-combobox-popup" style="display:none">
            <ul class="z-combobox-content">
              <li class="z-comboitem" onclick="pick('cb-pd','05-JUN-2026')"><span class="z-comboitem-text">05-JUN-2026</span></li>
              <li class="z-comboitem" onclick="pick('cb-pd','19-JUN-2026')"><span class="z-comboitem-text">19-JUN-2026</span></li>
            </ul>
          </div>
        </span>
      </div></td>
    </tr>
    <tr class="z-row">
      <td class="z-row-inner"><div class="z-row-content"><span class="z-label">Record Type</span></div></td>
      <td class="z-row-inner"><div class="z-row-content">
        <span id="cb-rt" class="z-combobox z-combobox-readonly" role="combobox" aria-owns="cb-rt-pp">
          <input id="cb-rt-real" class="z-combobox-input" readonly value="">
          <a id="cb-rt-btn" class="z-combobox-button" role="button" onclick="toggleCb('cb-rt')">
            <i class="z-combobox-icon z-icon-caret-down"></i></a>
          <div id="cb-rt-pp" class="z-combobox-popup" style="display:none">
            <ul class="z-combobox-content">
              <li class="z-comboitem" onclick="pick('cb-rt','TXN')"><span class="z-comboitem-text">TXN</span></li>
              <li class="z-comboitem" onclick="pick('cb-rt','OTS')"><span class="z-comboitem-text">OTS</span></li>
            </ul>
          </div>
        </span>
      </div></td>
    </tr>
    <tr class="z-row">
      <td class="z-row-inner"><div class="z-row-content"><span class="z-label">Core System</span></div></td>
      <td class="z-row-inner"><div class="z-row-content">
        <select id="sel-cs"><option value="">--</option><option value="FLEX">FLEX</option><option value="UBS">UBS</option></select>
      </div></td>
    </tr>
    <tr class="z-row">
      <td class="z-row-inner"><div class="z-row-content"><span class="z-label">Cycle No</span></div></td>
      <td class="z-row-inner"><div class="z-row-content">
        <!-- Lazy combobox: the popup does NOT exist until the button is clicked,
             and is then rendered at document.body — the real ZK behaviour that
             made popup_sel empty in the field. -->
        <span id="cb-cy" class="z-combobox z-combobox-readonly" role="combobox" aria-owns="cb-cy-pp">
          <input id="cb-cy-real" class="z-combobox-input" readonly value="">
          <a id="cb-cy-btn" class="z-combobox-button" role="button" onclick="openLazy()">
            <i class="z-combobox-icon z-icon-caret-down"></i></a>
        </span>
      </div></td>
    </tr>
  </tbody></table></div>

  <button type="button" id="fetchbtn" class="z-button" onclick="window.__fired.fetch = true">Fetch</button>

  <script>
    window.__fired = {ebp: false, fetch: false};
    function toggleEcb() {
      var row = document.getElementById('r-ebp');
      row.style.display = '';
      document.getElementById('r-ecb').setAttribute('aria-expanded', 'true');
      document.getElementById('r-ecb-icon').className = 'z-icon-caret-down z-tree-open';
    }
    function toggleCb(id) {
      var pp = document.getElementById(id + '-pp');
      pp.style.display = (pp.style.display === 'none' || !pp.style.display) ? 'block' : 'none';
    }
    function pick(id, val) {
      document.getElementById(id + '-real').value = val;
      document.getElementById(id + '-pp').style.display = 'none';
    }
    function openLazy() {
      if (document.getElementById('cb-cy-pp')) return;  // already open
      var pp = document.createElement('div');
      pp.id = 'cb-cy-pp'; pp.className = 'z-combobox-popup'; pp.style.display = 'block';
      pp.innerHTML = '<ul class="z-combobox-content">' +
        '<li class="z-comboitem"><span class="z-comboitem-text">05</span></li>' +
        '<li class="z-comboitem"><span class="z-comboitem-text">06</span></li></ul>';
      pp.querySelectorAll('.z-comboitem').forEach(function (li) {
        li.onclick = function () {
          document.getElementById('cb-cy-real').value = li.querySelector('.z-comboitem-text').textContent;
          pp.parentNode.removeChild(pp);
        };
      });
      document.body.appendChild(pp);  // rendered at body, like real ZK
    }
  </script>
</body></html>"""


@contextlib.asynccontextmanager
async def _page() -> AsyncIterator[tuple[Any, Any]]:
    pw = await async_playwright().start()
    browser = None
    try:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium not available for ZK form/nav test: {e}")
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content(_PAGE, wait_until="domcontentloaded")
        yield page, context
    finally:
        if browser is not None:
            await browser.close()
        await pw.stop()


def _ctx(session: PlaywrightBrowserSession) -> CapabilityContext:
    class _NoopCM:
        async def __aexit__(self, *a: object) -> None:  # pragma: no cover
            return None

    state: dict[str, Any] = {stash_key(session.id): SessionHolder(cm=_NoopCM(), session=session)}
    return CapabilityContext(session_state=state, run_id="test-run")


async def test_tree_select_expands_parent_and_clicks_leaf() -> None:
    async with _page() as (page, context):
        session = PlaywrightBrowserSession(_id="zk-tree", page=page, context=context)
        ctx = _ctx(session)

        # The leaf is hidden until the parent caret is expanded.
        assert await page.eval_on_selector("#r-ebp", "el => getComputedStyle(el).display") == "none"

        out = await web_tree_select.run(
            ctx, {"session": session.id, "path": ["E-Callback Processing", "Ecall Back Processing"]}
        )

        assert out["selected"] == "Ecall Back Processing"
        assert out["expanded"] == ["E-Callback Processing"]
        assert (await page.evaluate("() => window.__fired.ebp")) is True


async def test_select_zk_combobox_record_type() -> None:
    async with _page() as (page, context):
        session = PlaywrightBrowserSession(_id="zk-rt", page=page, context=context)
        ctx = _ctx(session)
        out = await web_select.run(ctx, {"session": session.id, "label": "Record Type", "value": "TXN"})
        assert out["kind"] == "zk_combobox"
        assert (await page.input_value("#cb-rt-real")) == "TXN"


async def test_select_zk_combobox_processing_date_typo_label() -> None:
    """The real label is misspelt 'Processsing Date' — match it verbatim."""
    async with _page() as (page, context):
        session = PlaywrightBrowserSession(_id="zk-pd", page=page, context=context)
        ctx = _ctx(session)
        await web_select.run(ctx, {"session": session.id, "label": "Processsing Date", "value": "19-JUN-2026"})
        assert (await page.input_value("#cb-pd-real")) == "19-JUN-2026"


async def test_select_zk_combobox_lazy_popup_rendered_at_body() -> None:
    """Reproduces the field failure: the popup is created lazily (at document.body)
    only when the combobox opens, so it doesn't exist at resolve time. web_select
    must still find and pick the item without a 'selector is empty' crash."""
    async with _page() as (page, context):
        session = PlaywrightBrowserSession(_id="zk-lazy", page=page, context=context)
        ctx = _ctx(session)
        # Confirm the popup genuinely doesn't exist before opening.
        assert await page.eval_on_selector_all("#cb-cy-pp", "els => els.length") == 0
        out = await web_select.run(ctx, {"session": session.id, "label": "Cycle No", "value": "06"})
        assert out["kind"] == "zk_combobox"
        assert (await page.input_value("#cb-cy-real")) == "06"


async def test_select_native_select_fallback() -> None:
    async with _page() as (page, context):
        session = PlaywrightBrowserSession(_id="zk-cs", page=page, context=context)
        ctx = _ctx(session)
        out = await web_select.run(ctx, {"session": session.id, "label": "Core System", "value": "FLEX"})
        assert out["kind"] == "select"
        assert (await page.input_value("#sel-cs")) == "FLEX"


async def test_select_unknown_option_raises() -> None:
    async with _page() as (page, context):
        session = PlaywrightBrowserSession(_id="zk-x", page=page, context=context)
        ctx = _ctx(session)
        with pytest.raises(RuntimeError, match="no option matching"):
            await web_select.run(
                ctx, {"session": session.id, "label": "Record Type", "value": "NOPE", "timeout_ms": 1500}
            )


async def test_fetch_is_clickable_via_web_click_text() -> None:
    """Fetch is a <button class="z-button"> — cap.web_click(text) handles it."""
    async with _page() as (page, context):
        session = PlaywrightBrowserSession(_id="zk-fetch", page=page, context=context)
        ctx = _ctx(session)
        await web_click.run(ctx, {"session": session.id, "text": "Fetch"})
        assert (await page.evaluate("() => window.__fired.fetch")) is True
