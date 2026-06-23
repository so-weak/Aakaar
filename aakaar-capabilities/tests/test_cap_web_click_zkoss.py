"""Real-Chromium tests for cap.web_click against the CTS Outward ZK landing page.

The CTS Outward ("ExpressClear") main window renders Logout as an icon-only ZK
toolbar button — an ``<a class="z-toolbarbutton">`` wrapping an ``<img
src="/outward/images/logout.png" alt="" aria-hidden="true">``. There is no
"logout" text anywhere, which is exactly why the CTSOutTrial run failed in the
field with ``browser.click_by_text: nothing matches 'logout'``.

These tests load a faithful reproduction of that DOM in real headless Chromium
and assert:
  * ``browser.click_by_text("logout")`` genuinely finds nothing (the gap), and
  * ``cap.web_click(image="logout")`` resolves the icon by its image src, clicks
    the clickable ancestor, and the ZK-style delegated handler fires;
  * ``text`` mode clicks a labelled button (the post-login "OK" dialog), and
  * ``selector`` mode clicks an explicit element.

Skips cleanly when Chromium isn't installed, so CI without a browser stays green.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytest.importorskip("playwright.async_api")
from aakaar_caps.browser.playwright import PlaywrightBrowserSession  # noqa: E402
from aakaar_caps.browser.state import SessionHolder, stash_key  # noqa: E402
from aakaar_caps.caps import web_click  # noqa: E402
from aakaar_caps.context import CapabilityContext  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402


# Faithful reproduction of the CTS Outward main window's relevant controls:
#   * the title-bar Logout toolbar button (icon-only, empty alt, aria-hidden img,
#     dynamic ZK id) — copied from the page source the operator inspected;
#   * a post-login "OK" message-box button (ZK <button class="z-button">);
#   * a menu treerow ("Outward") to exercise text climbing to a clickable row.
# The img carries an explicit size so a broken-image src still has layout (the
# real page loads the icon from the server, so it always has size).
_CTS_MAIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8"></head>
<body class="webkit chrome silvertail">
  <div id="e1EF0" class="z-window z-window-modal" role="dialog" aria-modal="true">
    <div id="e1EF0-cap" class="z-window-header">
      <div class="mainCaption z-caption">
        ExpressClear - CTS Ver : 3.1.0
        <div id="e1EF8" class="captionButtons z-div" align="center">
          <a id="e1EF9" class="z-toolbarbutton" tabindex="0" role="button">
            <span id="e1EF9-cnt" class="z-toolbarbutton-content">
              <img src="/outward/images/logout.png" align="absmiddle" alt=""
                   aria-hidden="true" style="width:16px;height:16px;display:inline-block">
            </span>
          </a>
        </div>
      </div>
    </div>
    <div id="e1EF0-cave" class="z-window-content">
      <div class="z-tree" role="treegrid"><table><tbody class="z-treechildren">
        <tr id="e1EFo0" class="z-treerow" role="row">
          <td class="z-treecell"><div class="z-treecell-content">
            <span class="z-label">Outward</span>
          </div></td>
        </tr>
      </tbody></table></div>
      <button id="okbtn" type="button" class="z-button">OK</button>
    </div>
  </div>
  <div id="e1EF0-mask" class="z-modal-mask">
    <button id="e1EF0-mask-a" style="top:0;left:0;width:0;height:0" onclick="return false;"
            class="z-focus-a" aria-hidden="true" tabindex="-1"></button>
  </div>
  <script>
    window.__fired = {logout: false, ok: false, outward: false};
    // ZK binds delegated handlers on the widget element; mirror that on the <a>.
    document.getElementById('e1EF9').addEventListener('click', () => { window.__fired.logout = true; });
    document.getElementById('okbtn').addEventListener('click', () => { window.__fired.ok = true; });
    document.getElementById('e1EFo0').addEventListener('click', () => { window.__fired.outward = true; });
  </script>
</body></html>"""


@contextlib.asynccontextmanager
async def _page(html: str) -> AsyncIterator[tuple[Any, Any]]:
    pw = await async_playwright().start()
    browser = None
    try:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium not available for ZK web_click test: {e}")
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content(html, wait_until="domcontentloaded")
        yield page, context
    finally:
        if browser is not None:
            await browser.close()
        await pw.stop()


def _ctx_for(session: PlaywrightBrowserSession) -> CapabilityContext:
    """A CapabilityContext whose session_state holds `session` (no pool / store
    needed — cap.web_click only touches the session)."""

    class _NoopCM:
        async def __aexit__(self, *a: object) -> None:  # pragma: no cover
            return None

    state: dict[str, Any] = {stash_key(session.id): SessionHolder(cm=_NoopCM(), session=session)}
    return CapabilityContext(session_state=state, run_id="test-run")


async def test_click_by_text_logout_finds_nothing() -> None:
    """The exact field failure: there is no 'logout' text, so click_by_text — the
    generic primitive — raises. This is what cap.web_click exists to fix."""
    async with _page(_CTS_MAIN_HTML) as (page, context):
        session = PlaywrightBrowserSession(_id="cts-1", page=page, context=context)
        with pytest.raises(RuntimeError, match="nothing matches"):
            await session.click_by_text("logout")


async def test_web_click_image_logs_out() -> None:
    """cap.web_click(image='logout') resolves the icon-only toolbar button by its
    image src and clicks the clickable <a> ancestor — the ZK handler fires."""
    async with _page(_CTS_MAIN_HTML) as (page, context):
        session = PlaywrightBrowserSession(_id="cts-2", page=page, context=context)
        ctx = _ctx_for(session)

        out = await web_click.run(ctx, {"session": session.id, "image": "logout"})

        assert out["clicked"] is True
        assert out["matched_by"] == "image"
        # The clicked element is the toolbar anchor, not the bare <img>.
        assert out["selector"] == "#e1EF9"
        fired = await page.evaluate("() => window.__fired")
        assert fired["logout"] is True, "logout handler never fired"


async def test_web_click_image_partial_src_match() -> None:
    """A partial asset-name match ('logout.png') also resolves the control."""
    async with _page(_CTS_MAIN_HTML) as (page, context):
        session = PlaywrightBrowserSession(_id="cts-3", page=page, context=context)
        ctx = _ctx_for(session)
        await web_click.run(ctx, {"session": session.id, "image": "logout.png"})
        assert (await page.evaluate("() => window.__fired.logout")) is True


async def test_web_click_text_clicks_ok_button() -> None:
    """text mode clicks the post-login 'OK' message-box button by its label."""
    async with _page(_CTS_MAIN_HTML) as (page, context):
        session = PlaywrightBrowserSession(_id="cts-4", page=page, context=context)
        ctx = _ctx_for(session)
        out = await web_click.run(ctx, {"session": session.id, "text": "OK"})
        assert out["matched_by"] == "text"
        assert (await page.evaluate("() => window.__fired.ok")) is True


async def test_web_click_selector_clicks_explicit() -> None:
    """selector mode clicks an explicit CSS target (highest priority)."""
    async with _page(_CTS_MAIN_HTML) as (page, context):
        session = PlaywrightBrowserSession(_id="cts-5", page=page, context=context)
        ctx = _ctx_for(session)
        out = await web_click.run(ctx, {"session": session.id, "selector": "#okbtn"})
        assert out["matched_by"] == "selector"
        assert (await page.evaluate("() => window.__fired.ok")) is True


async def test_web_click_missing_target_raises() -> None:
    """A target that isn't on the page fails fast (short timeout) rather than
    hanging — the executor surfaces this as a node failure."""
    async with _page(_CTS_MAIN_HTML) as (page, context):
        session = PlaywrightBrowserSession(_id="cts-6", page=page, context=context)
        ctx = _ctx_for(session)
        with pytest.raises(RuntimeError, match="no clickable control matched"):
            await web_click.run(
                ctx, {"session": session.id, "image": "does-not-exist", "timeout_ms": 1000}
            )
