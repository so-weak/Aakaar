"""Playwright-backed BrowserSession + Pool.

Imported lazily — Playwright (and Chromium) is heavy. If the package is
not installed or browsers are not provisioned, importing this module
raises only when it's actually constructed, never at import time.

For v1: per-run isolation, no warm pool, headless by default. The warm
pool is a Phase 2 optimization once we have measurements.

This module is NOT exercised by the default test suite — those tests use
`FakeBrowserPool`. To run a smoke test against real Chromium:
  AAKAR_RUN_PLAYWRIGHT_TESTS=1 pytest -k playwright_smoke
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from aakar.workers.browser.session import (
    BrowserSession,
    DownloadedFile,
    ExtractedValue,
)


logger = logging.getLogger(__name__)


# ---------- find-labeled-field resolver -------------------------------------
#
# Resolves a free-form label string to the controlling form element on the
# page, returning a unique CSS selector + the bits the caller needs to
# dispatch (tag, type, select options, matched radio selector). All in JS so
# the round-trip is one Playwright call. Used by `set_field`.
_FIND_LABELED_FIELD_JS = r"""
(({label, value}) => {
  function cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/([^a-zA-Z0-9_-])/g, "\\$1");
  }
  function pathSelector(el) {
    if (!el) return null;
    if (el.id) return "#" + cssEscape(el.id);
    const parts = [];
    let cur = el;
    while (cur && cur !== document.body) {
      const parent = cur.parentElement;
      if (!parent) break;
      const same = Array.from(parent.children).filter(
        (c) => c.tagName === cur.tagName
      );
      const idx = same.indexOf(cur);
      const seg =
        same.length === 1
          ? cur.tagName.toLowerCase()
          : cur.tagName.toLowerCase() + ":nth-of-type(" + (idx + 1) + ")";
      parts.unshift(seg);
      cur = parent;
      if (parts.length > 7) break;
    }
    return parts.join(" > ");
  }
  function visible(el) {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }
  function fieldOf(label) {
    const forId = label.getAttribute("for");
    if (forId) {
      const t = document.getElementById(forId);
      if (t) return t;
    }
    const child = label.querySelector("input, select, textarea");
    if (child) return child;
    let sib = label.nextElementSibling;
    while (sib) {
      if (sib.matches("input, select, textarea")) return sib;
      const inner = sib.querySelector("input, select, textarea");
      if (inner) return inner;
      sib = sib.nextElementSibling;
    }
    let parent = label.parentElement;
    let depth = 0;
    while (parent && depth < 4) {
      const inner = parent.querySelector("input, select, textarea");
      if (inner && !inner.closest("label")) return inner;
      parent = parent.parentElement;
      depth++;
    }
    return null;
  }
  const needle = String(label || "").trim().toLowerCase();
  if (!needle) return { found: false, hint: "empty label" };

  const labels = Array.from(
    document.querySelectorAll("label, [class*='label']")
  ).filter(visible);
  const exact = labels.filter((l) => l.innerText.trim().toLowerCase() === needle);
  const sub = labels.filter((l) => l.innerText.trim().toLowerCase().includes(needle));
  const ordered = exact.length ? exact : sub;

  for (const lbl of ordered) {
    const target = fieldOf(lbl);
    if (!target) continue;
    const tag = target.tagName.toLowerCase();
    const type = (target.getAttribute("type") || "").toLowerCase();
    const result = {
      found: true,
      tag,
      type,
      selector: pathSelector(target),
    };
    if (tag === "select") {
      result.options = Array.from(target.options).map((o) => ({
        label: o.label,
        value: o.value,
        text: o.text,
      }));
    }
    if (tag === "input" && type === "radio" && target.name) {
      const wanted = String(value || "").trim().toLowerCase();
      const radios = Array.from(
        document.querySelectorAll(
          "input[type='radio'][name=" + JSON.stringify(target.name) + "]"
        )
      );
      let chosen = null;
      for (const r of radios) {
        const v = (r.getAttribute("value") || "").trim().toLowerCase();
        if (v === wanted) { chosen = r; break; }
        const owner = r.closest("label");
        if (owner && owner.innerText.trim().toLowerCase().includes(wanted)) {
          chosen = r; break;
        }
      }
      if (chosen) result.radio_match_selector = pathSelector(chosen);
    }
    return result;
  }
  return { found: false, hint: "labels found: " + ordered.length };
})
""".strip()


@dataclass
class PlaywrightBrowserSession(BrowserSession):
    """Wraps a Playwright BrowserContext + Page pair.

    Use `PlaywrightBrowserPool.checkout()` rather than constructing this
    directly so lifecycle is handled correctly.
    """

    _id: str
    page: Any  # playwright.async_api.Page (kept untyped to avoid hard import)
    context: Any  # playwright.async_api.BrowserContext

    @property
    def id(self) -> str:
        return self._id

    async def navigate(self, url: str) -> None:
        await self.page.goto(url)

    async def wait_for(
        self,
        selector: str,
        timeout_ms: int = 30000,
        state: str = "attached",
    ) -> None:
        await self.page.wait_for_selector(selector, timeout=timeout_ms, state=state)

    async def fill(self, selector: str, value: str) -> None:
        await self.page.fill(selector, value)

    async def click(self, selector: str) -> None:
        await self.page.click(selector)

    async def click_by_text(self, text: str) -> None:
        # Try the most semantic locators first — links / buttons named
        # `text` — falling back to plain text-content matching. This
        # ordering avoids picking up a stray <span> with the same
        # innerText when there's a real link with that name.
        # `title=` and `aria-label=` cover icon-only buttons (e.g.
        # admin-app's logout is an avatar button with title="Logout").
        for locator in (
            self.page.get_by_role("link", name=text, exact=False),
            self.page.get_by_role("button", name=text, exact=False),
            self.page.locator(f"[title*={text!r} i]"),
            self.page.locator(f"[aria-label*={text!r} i]"),
            self.page.get_by_text(text, exact=False),
        ):
            try:
                if await locator.count() >= 1:
                    await locator.first.click()
                    return
            except Exception:  # noqa: BLE001
                continue
        raise RuntimeError(f"click_by_text: nothing matches {text!r}")

    async def select(self, selector: str, value: str) -> None:
        await self.page.select_option(selector, value)

    async def set_field(self, label: str, value: str) -> None:
        """Locate a form control by its visible label and set its value.

        Uses a custom JS resolver (not just Playwright's `get_by_label`)
        because real-world forms — admin-app's recon page included —
        often have unbound labels (`<label>Switch Type</label>` next to
        a `<select>` with no `for=id` link, no wrapping). The resolver
        walks: label-with-`for` → label's child input → label's next
        sibling input → enclosing `.field` / `.row` container's first
        input.

        Once located, dispatch by tag name:
          - select  → selectOption (verbatim value, then
                      case-insensitive option-text match)
          - input[type=radio] → click the radio matching `value`
          - input[type=checkbox] → set to truthy/falsy of `value`
          - everything else → fill `value`
        """
        info = await self.page.evaluate(_FIND_LABELED_FIELD_JS, {"label": label, "value": value})
        if not isinstance(info, dict) or not info.get("found"):
            raise RuntimeError(
                f"set_field: no field labeled {label!r} on the page; "
                f"hint: {info.get('hint') if isinstance(info, dict) else None}"
            )
        tag = info.get("tag")
        sel = info.get("selector")
        if not isinstance(sel, str) or not sel:
            raise RuntimeError(f"set_field: resolver returned no selector for {label!r}")

        loc = self.page.locator(sel).first
        if tag == "select":
            try:
                await loc.select_option(value)
                return
            except Exception:
                pass
            options = info.get("options") or []
            wanted = value.strip().lower()
            for o in options:
                for cand in (o.get("value"), o.get("label"), o.get("text")):
                    if isinstance(cand, str) and cand.strip().lower() == wanted:
                        await loc.select_option(cand)
                        return
            raise RuntimeError(
                f"set_field: <select> {label!r} has no option matching {value!r}; "
                f"options: {[o.get('label') or o.get('value') for o in options]}"
            )

        if tag == "input":
            input_type = (info.get("type") or "").lower()
            if input_type == "radio":
                # Look for a radio in the same group with matching value
                # or label text.
                wanted = value.strip().lower()
                radio_sel = info.get("radio_match_selector")
                if radio_sel:
                    await self.page.locator(radio_sel).first.check()
                    return
                # As a last resort: any radio whose visible label matches
                role_loc = self.page.get_by_role("radio", name=value, exact=False)
                if await role_loc.count() >= 1:
                    await role_loc.first.check()
                    return
                raise RuntimeError(
                    f"set_field: no radio in group {label!r} matches {value!r}"
                )
            if input_type == "checkbox":
                want_on = value.strip().lower() in ("yes", "true", "1", "on", "checked")
                if want_on:
                    await loc.check()
                else:
                    await loc.uncheck()
                return

        await loc.fill(value)

    async def upload(self, selector: str, file_path: str) -> None:
        await self.page.set_input_files(selector, file_path)

    async def download(
        self, *, trigger_selector: str | None = None, url: str | None = None
    ) -> DownloadedFile:
        if trigger_selector and url:
            raise ValueError("download accepts trigger_selector OR url, not both")
        if trigger_selector:
            async with self.page.expect_download() as info:
                await self.page.click(trigger_selector)
            download = await info.value
            path = await download.path()
            with open(path, "rb") as f:
                content = f.read()
            return DownloadedFile(filename=download.suggested_filename, content=content)
        if url:
            response = await self.page.context.request.get(url)
            return DownloadedFile(
                filename=url.rsplit("/", 1)[-1] or "download.bin",
                content=await response.body(),
            )
        raise ValueError("download requires trigger_selector or url")

    async def extract(self, selector: str, attribute: str = "text") -> ExtractedValue:
        if attribute == "text":
            value = await self.page.inner_text(selector)
        elif attribute == "html":
            value = await self.page.inner_html(selector)
        else:
            value = await self.page.get_attribute(selector, attribute) or ""
        return ExtractedValue(value=value)

    async def screenshot(self) -> bytes:
        return await self.page.screenshot(full_page=True)

    async def screenshot_element(self, selector: str) -> bytes:
        return await self.page.locator(selector).screenshot()

    async def evaluate(self, js: str) -> object:
        return await self.page.evaluate(js)

    async def close(self) -> None:
        try:
            await self.context.close()
        except Exception:
            # Closing twice or after the browser process died — ignore.
            logger.debug("playwright: context.close() raised (ignored)", exc_info=True)


@dataclass
class PlaywrightBrowserPool:
    """Per-run, headless-by-default Chromium pool.

    Phase-2 work: warm pool, headed mode (Xvfb), per-tenant proxy.
    """

    headless: bool = True
    _playwright: Any = field(default=None, init=False)
    _browser: Any = field(default=None, init=False)

    async def _ensure_started(self) -> None:
        if self._browser is not None:
            return
        from playwright.async_api import async_playwright

        logger.info("playwright: starting Chromium (headless=%s)", self.headless)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        logger.debug("playwright: Chromium started")

    async def shutdown(self) -> None:
        if self._browser is not None:
            logger.info("playwright: shutting down Chromium")
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    @asynccontextmanager
    async def checkout(
        self, *, profile: str | None = None
    ) -> AsyncIterator[PlaywrightBrowserSession]:
        _ = profile  # v1: profiles ignored
        await self._ensure_started()
        context = await self._browser.new_context()
        page = await context.new_page()
        session = PlaywrightBrowserSession(
            _id=f"pw-{uuid.uuid4().hex[:8]}", page=page, context=context
        )
        logger.debug("playwright: checkout session=%s", session.id)
        try:
            yield session
        finally:
            logger.debug("playwright: closing session=%s", session.id)
            await session.close()
