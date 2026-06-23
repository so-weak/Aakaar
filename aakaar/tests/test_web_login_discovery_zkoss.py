"""Real-browser tests for cap.web_login discovery against a ZK/ZKoss login.

The other discovery tests drive a FakeBrowserSession with a canned descriptor,
so they exercise the Python wrapper but NOT the discovery JS itself. ZK/ZKoss
login screens are the case that broke in the field: they render a form-LESS
grid where the username, password and submit live in sibling cells with no
enclosing <form>. The old JS scoped its whole search to the password's parent
cell, found only the password, and failed with
``no_username_input_found`` / ``no_submit_button_found``.

These tests run the genuine ``DISCOVERY_JS`` in a real headless Chromium against
a faithful form-less ZK DOM and assert all three controls resolve — then drive
``fill`` + ``click`` through the real PlaywrightBrowserSession on the discovered
selectors and confirm the ZK login handler actually fires (both the
<button class="z-button"> and <a class="z-button"> shapes ZK emits).

Skips cleanly if Chromium isn't installed, so CI without a browser stays green.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import pytest

pytest.importorskip("playwright.async_api")
from aakaar_caps.browser.playwright import PlaywrightBrowserSession  # noqa: E402
from aakaar_caps.caps.web_login.discovery import (  # noqa: E402
    DISCOVERY_JS,
    LoginFormDescriptor,
)
from playwright.async_api import async_playwright  # noqa: E402

# Two ways ZK renders the login button: a real <button type="button"> widget and
# an anchor widget. Both carry the framework's `z-button` class and an id;
# crucially NEITHER is `type=submit`, and both sit in a table that is a SIBLING
# of the field grid — outside the username/password panel.
_ZK_BUTTON = '<button type="button" id="loginBtn" class="z-button" onclick="return zkLogin()">Login</button>'
_ZK_ANCHOR = '<a id="loginBtn" class="z-button" href="javascript:;" onclick="return zkLogin()">Login</a>'


def _zk_login_html(submit_html: str) -> str:
    """Faithful reproduction of the real CTS ZK login DOM.

    Structure that matters:
      * No <form> anywhere.
      * Username (#usertb) and password (#pwdtb) live in a ``<tbody class=
        "z-rows">`` grid, each inside its own ``<div class="z-row-content">``
        cell (the password cell mirrors the field's ``id="zI5Bh-cell"``).
      * The Login button is ``type="button"`` (NOT submit) and sits in a
        SEPARATE ``<table>`` that is a sibling of the grid, not inside z-rows.
      * Everything is wrapped in ``z-window-content`` / ``role="dialog"``.
      * A ZK modal mask contributes an offscreen ``class="z-focus-a"`` /
        ``aria-hidden`` / ``tabindex=-1`` button that must NOT be picked.
    """
    return f"""<!doctype html>
<html><head><meta charset="utf-8"></head>
<body class="webkit chrome">
  <div id="win" role="dialog" aria-modal="true" class="expstyle1 z-window z-window-modal">
    <div id="win-cap" class="z-window-header">ExpressClear CTS Outward - Web Access</div>
    <div id="win-cave" class="z-window-content">
      <div id="panel" class="z-div">
        <div class="z-grid" role="grid"><div class="z-grid-body">
          <table width="100%"><tbody class="z-rows" role="rowgroup">
            <tr class="z-row" role="row">
              <td class="z-row-inner" role="gridcell"><div class="z-row-content"><span class="z-label">Name :</span></div></td>
              <td class="z-row-inner" role="gridcell"><div id="uin-cell" class="z-row-content"><input type="text" id="usertb" style="width:95%;border:1px solid #d8d0d0"></div></td>
            </tr>
            <tr class="z-row" role="row">
              <td class="z-row-inner" role="gridcell"><div class="z-row-content"><span class="z-label">Password :</span></div></td>
              <td class="z-row-inner" role="gridcell"><div id="zI5Bh-cell" class="z-row-content"><input type="password" id="pwdtb" style="width:95%;border:1px solid #d8d0d0"></div></td>
            </tr>
          </tbody></table>
        </div></div>
        <table class="z-hbox" width="100%"><tbody><tr valign="top"><td align="center">
          <table height="100%"><tbody><tr valign="top"><td>{submit_html}</td></tr></tbody></table>
        </td></tr></tbody></table>
        <table class="z-hbox" width="100%"><tbody><tr valign="top"><td align="center">
          <span class="z-label">Copyright © 2005-2015 Image InfoSystems Private Limited.</span>
        </td></tr></tbody></table>
      </div>
    </div>
  </div>
  <div id="win-mask" class="z-modal-mask">
    <button id="win-mask-a" style="top:0;left:0;width:0;height:0" onclick="return false;"
            class="z-focus-a" aria-hidden="true" tabindex="-1"></button>
  </div>
  <script>
    window.__login = {{clicked: false, user: null, pass: null}};
    function zkLogin() {{
      window.__login.clicked = true;
      window.__login.user = document.getElementById('usertb').value;
      window.__login.pass = document.getElementById('pwdtb').value;
      return false;  // ZK posts via AU; don't navigate in the test
    }}
  </script>
</body></html>"""


@contextlib.asynccontextmanager
async def _zk_page(html: str) -> AsyncIterator[tuple]:
    """Launch a real headless Chromium, load `html`, yield (page, context).

    Skips the test (not fails) when the Chromium binary isn't installed."""
    pw = await async_playwright().start()
    browser = None
    try:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium not available for ZKoss browser test: {e}")
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content(html, wait_until="domcontentloaded")
        yield page, context
    finally:
        if browser is not None:
            await browser.close()
        await pw.stop()


@pytest.mark.parametrize("submit_html", [_ZK_BUTTON, _ZK_ANCHOR], ids=["zk-button", "zk-anchor"])
async def test_discovery_resolves_formless_zkoss_login(submit_html: str) -> None:
    """The real discovery JS finds username + password + submit on a form-less
    ZK page — the exact case the narrow-scope heuristic used to miss."""
    async with _zk_page(_zk_login_html(submit_html)) as (page, _context):
        result = await page.evaluate(DISCOVERY_JS)
        desc = LoginFormDescriptor.from_js_result(result)

        # All three controls resolved — including the submit, which lives in a
        # sibling table outside the field grid and is only type="button".
        assert desc.password_selector == "#pwdtb"
        assert desc.username_selector == "#usertb"
        assert desc.submit_selector == "#loginBtn"

        # The ambiguity flags that broke the field run are gone.
        assert "no_username_input_found" not in desc.ambiguity_reasons
        assert "no_submit_button_found" not in desc.ambiguity_reasons

        # Every selector matches exactly one live element (so Playwright's
        # document-rooted fill/click/wait_for will hit the right node).
        for sel in (desc.username_selector, desc.password_selector, desc.submit_selector):
            count = await page.eval_on_selector_all(sel, "els => els.length")
            assert count == 1, f"{sel!r} matched {count} elements, expected 1"

        # The submit is NOT the offscreen modal-mask focus button.
        assert desc.submit_selector != "#win-mask-a"

        # The LLM-fallback snapshot carries the field panel AND the submit
        # element (which sits outside that panel) for the model to see.
        assert "usertb" in desc.form_outer_html_excerpt
        assert "loginBtn" in desc.form_outer_html_excerpt


@pytest.mark.parametrize("submit_html", [_ZK_BUTTON, _ZK_ANCHOR], ids=["zk-button", "zk-anchor"])
async def test_zkoss_discovered_selectors_fill_and_click(submit_html: str) -> None:
    """End-to-end on ZK: discover selectors, then fill + click them through the
    real PlaywrightBrowserSession and confirm the ZK login handler fired with
    the typed credentials. This proves the click lands on the ZK button widget."""
    async with _zk_page(_zk_login_html(submit_html)) as (page, context):
        desc = LoginFormDescriptor.from_js_result(await page.evaluate(DISCOVERY_JS))
        assert desc.username_selector and desc.password_selector and desc.submit_selector

        session = PlaywrightBrowserSession(_id="zk-test", page=page, context=context)
        await session.fill(desc.username_selector, "alice@bank.test")
        await session.fill(desc.password_selector, "s3cr3t!")
        await session.click(desc.submit_selector)

        state = await page.evaluate("() => window.__login")
        assert state["clicked"] is True, "ZK login button handler never fired"
        assert state["user"] == "alice@bank.test"
        assert state["pass"] == "s3cr3t!"


async def test_password_only_formless_page_preserves_fallback() -> None:
    """A form-less page with a password but no username must still anchor on the
    password (scope falls back to its parent) and flag the missing username,
    rather than crashing — the password-only fallback branch."""
    html = """<!doctype html><html><body>
      <div class="z-window"><div class="z-row-content">
        <input type="password" id="pwdtb" class="z-textbox">
      </div></div>
    </body></html>"""
    async with _zk_page(html) as (page, _context):
        desc = LoginFormDescriptor.from_js_result(await page.evaluate(DISCOVERY_JS))
        assert desc.password_selector == "#pwdtb"
        assert desc.username_selector is None
        assert "no_username_input_found" in desc.ambiguity_reasons
