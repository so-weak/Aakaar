"""Proof that the ZK caps are independent of ZK's per-render element ids.

ZK assigns volatile ids (e1EF9, e1EF2c0, …) that change every page load. This
test loads a CTS-like page, then **rewrites every element id to a brand-new
value** before acting — simulating a different render than any the DAG was
authored against. The page's event handlers use relative DOM (no getElementById),
so the rewrite breaks nothing except hardcoded-id assumptions. All four
interactions still succeed, because the caps resolve by image/text/aria-label/
field-label and compute the (current) selector live, immediately before clicking.

Skips cleanly when Chromium isn't installed.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytest.importorskip("playwright.async_api")
from aakaar_caps.browser.playwright import PlaywrightBrowserSession  # noqa: E402
from aakaar_caps.browser.state import SessionHolder, stash_key  # noqa: E402
from aakaar_caps.caps import web_click, web_select, web_tree_select  # noqa: E402
from aakaar_caps.context import CapabilityContext  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402


# Same widget shapes as the real portal, but every handler uses *relative* DOM
# so we can scramble ids without breaking the page itself.
_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
  .z-combobox-button{display:inline-block;width:18px;height:18px}
  .z-tree-icon{display:inline-block;width:18px;height:18px}
  .z-tree-icon i{display:inline-block;width:16px;height:16px}
  .z-comboitem{display:block;min-height:16px}.z-button{min-width:60px;min-height:20px}
  .z-treerow{display:table-row}.z-treerow .z-label{display:inline-block;min-height:14px}
</style></head>
<body>
  <!-- logout toolbar icon (id will be scrambled) -->
  <a id="logout-anchor" class="z-toolbarbutton" role="button" onclick="window.__f.logout=true">
    <span class="z-toolbarbutton-content">
      <img src="/outward/images/logout.png" alt="" aria-hidden="true" style="width:16px;height:16px;display:inline-block">
    </span>
  </a>

  <div class="z-tree" role="treegrid"><table><tbody class="z-treechildren">
    <tr id="t-parent" class="z-treerow" aria-expanded="false" aria-level="2" aria-label="E-Callback Processing">
      <td class="z-treecell"><div class="z-treecell-content">
        <span class="z-tree-icon" onclick="
          var row=this.closest('tr'); row.nextElementSibling.style.display='';
          row.setAttribute('aria-expanded','true'); this.querySelector('i').className='z-icon-caret-down z-tree-open';">
          <i class="z-icon-caret-right z-tree-close"></i></span>
        <div class="z-label"><span class="z-label">E-Callback Processing</span></div>
      </div></td>
    </tr>
    <tr id="t-child" class="z-treerow" style="display:none" aria-level="3" aria-label="Ecall Back Processing"
        onclick="window.__f.child=true">
      <td class="z-treecell"><div class="z-treecell-content">
        <span class="z-tree-line z-tree-spacer"></span>
        <div class="z-label"><span class="z-label">Ecall Back Processing</span></div>
      </div></td>
    </tr>
  </tbody></table></div>

  <div class="z-grid"><table><tbody class="z-rows">
    <tr class="z-row">
      <td><div class="z-row-content"><span class="z-label">Record Type</span></div></td>
      <td><div class="z-row-content">
        <span class="z-combobox z-combobox-readonly" role="combobox">
          <input class="z-combobox-input" readonly value="">
          <a class="z-combobox-button" role="button" onclick="
            var pp=this.parentElement.querySelector('.z-combobox-popup');
            pp.style.display=(pp.style.display==='none'||!pp.style.display)?'block':'none';">
            <i class="z-combobox-icon z-icon-caret-down"></i></a>
          <div class="z-combobox-popup" style="display:none"><ul class="z-combobox-content">
            <li class="z-comboitem" onclick="
              var c=this.closest('.z-combobox');
              c.querySelector('.z-combobox-input').value=this.querySelector('.z-comboitem-text').textContent;
              c.querySelector('.z-combobox-popup').style.display='none';">
              <span class="z-comboitem-text">TXN</span></li>
            <li class="z-comboitem" onclick="
              var c=this.closest('.z-combobox');
              c.querySelector('.z-combobox-input').value=this.querySelector('.z-comboitem-text').textContent;
              c.querySelector('.z-combobox-popup').style.display='none';">
              <span class="z-comboitem-text">OTS</span></li>
          </ul></div>
        </span>
      </div></td>
    </tr>
  </tbody></table></div>

  <button type="button" class="z-button" onclick="window.__f.fetch=true">Fetch</button>
  <script>window.__f = {logout:false, child:false, fetch:false};</script>
</body></html>"""

# Rewrite every id to a different value — as if ZK re-rendered with fresh ids.
_SCRAMBLE = """() => {
  let n = 0;
  document.querySelectorAll('[id]').forEach((el) => { el.id = 'zkRender2_' + (n++); });
  return n;
}"""


@contextlib.asynccontextmanager
async def _page() -> AsyncIterator[tuple[Any, Any]]:
    pw = await async_playwright().start()
    browser = None
    try:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium not available: {e}")
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
    return CapabilityContext(session_state=state, run_id="dyn-ids")


async def test_caps_work_after_every_id_is_rewritten() -> None:
    async with _page() as (page, context):
        session = PlaywrightBrowserSession(_id="dyn", page=page, context=context)
        ctx = _ctx(session)

        # Simulate a different render: every id is now something the DAG never saw.
        changed = await page.evaluate(_SCRAMBLE)
        assert changed > 0

        # All four resolve by stable semantics, not ids:
        await web_tree_select.run(
            ctx, {"session": session.id, "path": ["E-Callback Processing", "Ecall Back Processing"]}
        )
        await web_select.run(ctx, {"session": session.id, "label": "Record Type", "value": "TXN"})
        await web_click.run(ctx, {"session": session.id, "text": "Fetch"})
        await web_click.run(ctx, {"session": session.id, "image": "logout"})

        fired = await page.evaluate("() => window.__f")
        assert fired["child"] is True, "tree leaf not clicked after id rewrite"
        assert fired["fetch"] is True, "Fetch not clicked after id rewrite"
        assert fired["logout"] is True, "logout not clicked after id rewrite"
        # combobox value set despite the scrambled ids
        val = await page.eval_on_selector(".z-combobox-input", "el => el.value")
        assert val == "TXN", f"combobox not set after id rewrite, got {val!r}"
