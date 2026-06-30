"""Shared in-page JS helpers for the ZK-aware browser capabilities.

Underscore-prefixed so the capability loader skips it (it exposes no SPEC). The
``JS_HELPERS`` block defines ``cssEscape`` / ``visible`` / ``bestSelector`` and is
concatenated into each capability's IIFE so element resolution behaves
identically across them (and matches cap.web_click / the login-form discovery).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

# Function declarations injected at the top of each resolver IIFE.
JS_HELPERS = r"""
  function cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/([^a-zA-Z0-9_-])/g, "\\$1");
  }
  function uniqueOnPage(sel) {
    try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; }
  }
  function visible(el) {
    if (!el || !(el instanceof Element)) return false;
    const st = window.getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }
  function norm(s) { return (s || "").replace(/\s+/g, " ").trim().toLowerCase(); }
  function bestSelector(el) {
    if (!el || !(el instanceof Element)) return null;
    if (el.id) return "#" + cssEscape(el.id);
    const name = el.getAttribute && el.getAttribute("name");
    if (name) {
      const s = el.tagName.toLowerCase() + "[name=" + JSON.stringify(name) + "]";
      if (uniqueOnPage(s)) return s;
    }
    const tid = el.getAttribute && el.getAttribute("data-testid");
    if (tid) {
      const s = "[data-testid=" + JSON.stringify(tid) + "]";
      if (uniqueOnPage(s)) return s;
    }
    const path = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur.tagName !== "BODY" && cur.tagName !== "HTML") {
      if (cur.id) { path.unshift("#" + cssEscape(cur.id)); return path.join(" > "); }
      const parent = cur.parentElement;
      let step = cur.tagName.toLowerCase();
      if (parent) step += ":nth-child(" + (Array.from(parent.children).indexOf(cur) + 1) + ")";
      path.unshift(step);
      cur = parent;
    }
    return path.length ? path.join(" > ") : el.tagName.toLowerCase();
  }
"""


# In-page native click on a selector — the fallback when Playwright's actionable
# click can't land (ZK widgets occasionally fail the visibility/stability checks
# even though their delegated handler works fine on a native .click()).
_JS_CLICK = r"""
(() => {
  let el = null;
  try { el = document.querySelector(__SEL__); } catch (e) { el = null; }
  if (!el) return false;
  try { el.scrollIntoView({ block: "center", inline: "center" }); } catch (e) {}
  el.click();
  return true;
})()
"""


# Errors that mean "the page navigated / re-rendered while we were evaluating" —
# common with ZK AU updates (clicking a tree node / Fetch re-renders the page).
# These are transient: retry once the new execution context is ready.
_NAV_ERRORS = (
    "execution context was destroyed",
    "most likely because of a navigation",
    "navigation",
    "target closed",
    "frame was detached",
    "page closed",
    "context was destroyed",
)


async def safe_evaluate(sess: Any, js: str, *, retries: int = 15, delay: float = 0.3) -> Any:
    """Run ``sess.evaluate(js)`` but transparently retry when a ZK AU navigation
    destroys the JS execution context mid-evaluate (the new context comes back a
    moment later). Non-navigation errors propagate immediately."""
    last: Exception | None = None
    for _ in range(max(1, retries)):
        try:
            return await sess.evaluate(js)
        except Exception as e:  # noqa: BLE001
            if any(k in str(e).lower() for k in _NAV_ERRORS):
                last = e
                await asyncio.sleep(delay)
                continue
            raise
    if last is not None:
        raise last
    return None


async def click_or_js(sess: Any, selector: str) -> None:
    """Click ``selector`` via Playwright; on failure, dispatch a native in-page
    click. Raises only if the element can't be found for the JS fallback either."""
    try:
        await sess.click(selector)
        return
    except Exception as exc:  # noqa: BLE001
        ok = await safe_evaluate(sess, _JS_CLICK.replace("__SEL__", json.dumps(selector)))
        if not ok:
            raise RuntimeError(f"could not click {selector!r}") from exc
