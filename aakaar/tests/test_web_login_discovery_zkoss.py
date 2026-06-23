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

# Two ways ZK renders the login button: a real <button> widget and an
# anchor widget. Both carry the framework's `z-button` class and a generated
# id; neither is a `type=submit` and neither sits inside a <form>.
_ZK_BUTTON = '<button id="Login" class="z-button" onclick="return zkLogin()">Sign In</button>'
_ZK_ANCHOR = '<a id="Login" class="z-button" href="javascript:;" onclick="return zkLogin()">Sign In</a>'


def _zk_login_html(submit_html: str) -> str:
    """A form-less ZK/ZKoss login panel: grid rows of <div class="z-row-content">
    cells, the username and password in separate cells, the submit in a third.
    Mirrors the structure from the field failure (password cell was
    ``<div id="zI5Bh-cell" class="z-row-content">`` with only ``#pwdtb`` inside).
    """
    return f"""<!doctype html>
<html><head><meta charset="utf-8"></head>
<body>
  <div class="z-window" id="loginWin">
    <div class="z-window-header"><span class="z-label">CTS Outward — Sign In</span></div>
    <div class="z-grid"><div class="z-rows">
      <div class="z-row"><div class="z-row-inner">
        <div class="z-row-content"><span class="z-label">User ID</span></div>
        <div id="uid-cell" class="z-row-content">
          <input type="text" id="usertb" class="z-textbox" autocomplete="off">
        </div>
      </div></div>
      <div class="z-row"><div class="z-row-inner">
        <div class="z-row-content"><span class="z-label">Password</span></div>
        <div id="zI5Bh-cell" class="z-row-content">
          <input type="password" id="pwdtb" class="z-textbox">
        </div>
      </div></div>
      <div class="z-row"><div class="z-row-content z-row-actions">{submit_html}</div></div>
    </div></div>
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

        # All three controls resolved to their stable ids.
        assert desc.password_selector == "#pwdtb"
        assert desc.username_selector == "#usertb"
        assert desc.submit_selector == "#Login"

        # The ambiguity flags that broke the field run are gone.
        assert "no_username_input_found" not in desc.ambiguity_reasons
        assert "no_submit_button_found" not in desc.ambiguity_reasons

        # Every selector matches exactly one live element (so Playwright's
        # document-rooted fill/click/wait_for will hit the right node).
        for sel in (desc.username_selector, desc.password_selector, desc.submit_selector):
            count = await page.eval_on_selector_all(sel, "els => els.length")
            assert count == 1, f"{sel!r} matched {count} elements, expected 1"

        # The LLM-fallback snapshot is now the whole login panel, not just the
        # password cell — username + submit are present for the model to see.
        assert "usertb" in desc.form_outer_html_excerpt
        assert "Login" in desc.form_outer_html_excerpt


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
