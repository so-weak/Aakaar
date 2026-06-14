"""Per-plan Playwright session driver for the agentic planner.

Holds one browser session for the duration of a single plan() call.
Caller is responsible for `__aenter__` / `__aexit__` (or use the
`PlannerToolRunner.session()` async-context-manager). The runner exposes
four tool methods the dispatcher in `tools.py` calls.

It is deliberately thin: heuristics live in `cap.web_login.discovery`,
credential resolution lives in `aakaar.interpreter.credentials`. The
runner just wires them up to a planning session.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from aakaar.capabilities.web_login.discovery import discover_login_form
from aakaar.interpreter.credentials import fetch_credentials
from aakaar.shared.registry import Registry
from aakaar.vault import Vault

logger = logging.getLogger(__name__)


# How much visible text to stuff into an inspect_page result. The model
# pays input tokens for it; 2 KB is enough for a page summary without
# blowing the budget on 50 KB of footer text.
_TEXT_SNIPPET_LIMIT = 2000

# Cap the number of interactive elements returned per inspect call. A
# typical login or dashboard surface has under 60; anything more and
# we're either too high in the page or looking at a list view (in which
# case the model can navigate / filter).
_MAX_INTERACTIVE = 60


# JS the runner uses to stringify the page into a structured snapshot.
# Matches the field set the LLM sees as `inspect_page`'s result.
_INSPECT_JS = r"""
(() => {
  function visible(el) {
    if (!el || !(el instanceof Element)) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || +style.opacity === 0) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 1 && rect.height > 1;
  }
  function cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/([^a-zA-Z0-9_-])/g, "\\$1");
  }
  function isUniqueOnPage(sel, target) {
    try {
      const matches = document.querySelectorAll(sel);
      return matches.length === 1 && matches[0] === target;
    } catch { return false; }
  }
  function bestSelector(el) {
    if (!el || !(el instanceof Element)) return null;
    // Try increasingly specific candidates and accept the first that
    // resolves to *exactly this element*. A class-based selector that
    // matches three siblings is worse than the nth-of-type fallback.
    const candidates = [];
    if (el.id) candidates.push("#" + cssEscape(el.id));
    const name = el.getAttribute && el.getAttribute("name");
    if (name) {
      const valAttr = el.getAttribute && el.getAttribute("value");
      // Radios share a name; disambiguate with the value attribute.
      if (valAttr) {
        candidates.push(
          el.tagName.toLowerCase() +
          "[name=" + JSON.stringify(name) +
          "][value=" + JSON.stringify(valAttr) + "]"
        );
      }
      candidates.push(el.tagName.toLowerCase() + "[name=" + JSON.stringify(name) + "]");
    }
    const dataTestId = el.getAttribute && el.getAttribute("data-testid");
    if (dataTestId) candidates.push("[data-testid=" + JSON.stringify(dataTestId) + "]");
    const cls = (el.className || "").split(/\s+/).filter(Boolean).slice(0, 2).join(".");
    if (cls) {
      candidates.push(el.tagName.toLowerCase() + "." + cls.split(".").map(cssEscape).join("."));
    }
    for (const c of candidates) {
      if (isUniqueOnPage(c, el)) return c;
    }
    // Build a positional path from <body> down. Last-resort but always
    // resolves to exactly one element.
    const path = [];
    let cur = el;
    while (cur && cur.parentElement && cur !== document.body) {
      const parent = cur.parentElement;
      const sameTag = Array.from(parent.children).filter(
        (c) => c.tagName === cur.tagName
      );
      const idx = sameTag.indexOf(cur);
      const seg =
        sameTag.length === 1
          ? cur.tagName.toLowerCase()
          : cur.tagName.toLowerCase() + ":nth-of-type(" + (idx + 1) + ")";
      path.unshift(seg);
      cur = parent;
      if (path.length > 6) break;
    }
    return path.join(" > ") || el.tagName.toLowerCase();
  }
  function labelOf(el) {
    const aria = el.getAttribute && el.getAttribute("aria-label");
    if (aria) return aria.trim();
    if (el.id) {
      const lbl = document.querySelector("label[for=" + JSON.stringify(el.id) + "]");
      if (lbl && lbl.innerText) return lbl.innerText.trim();
    }
    const placeholder = el.getAttribute && el.getAttribute("placeholder");
    if (placeholder) return placeholder.trim();
    const inner = (el.innerText || el.textContent || "").trim();
    if (inner) return inner.slice(0, 120);
    const title = el.getAttribute && el.getAttribute("title");
    if (title) return title.trim();
    return "";
  }

  const interactiveSelector =
    "a[href], button, input:not([type='hidden']), select, textarea, [role='button'], [role='link']";
  const els = Array.from(document.querySelectorAll(interactiveSelector)).filter(visible);
  const items = els.slice(0, %MAX%).map((el) => {
    const tag = el.tagName.toLowerCase();
    const role =
      el.getAttribute("role") ||
      (tag === "input" ? `input[type=${(el.getAttribute("type") || "text")}]` : tag);
    return {
      tag,
      role,
      label: labelOf(el),
      selector: bestSelector(el),
      href: el.getAttribute("href") || null,
      type: el.getAttribute("type") || null,
      name: el.getAttribute("name") || null,
    };
  });

  const text = (document.body && document.body.innerText) || "";
  return {
    url: location.href,
    title: document.title || "",
    visible_text: text.slice(0, %TEXT%),
    interactive: items,
    interactive_truncated: els.length > items.length,
    interactive_count_total: els.length,
  };
})()
""".replace("%MAX%", str(_MAX_INTERACTIVE)).replace("%TEXT%", str(_TEXT_SNIPPET_LIMIT))


@dataclass
class PlannerToolRunner:
    """Owns one planning browser session for the lifetime of one plan."""

    browser_pool: Any
    vault: Vault
    tenant_id: UUID
    granted_capabilities: dict[str, dict[str, Any]]
    registry: Registry
    timeout_ms: int = 15000

    # Internal — populated by `session()`.
    _session: Any = field(default=None, init=False, repr=False)
    _cm: Any = field(default=None, init=False, repr=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[PlannerToolRunner]:
        """Open the planning browser, yield the runner, close on exit."""
        if self.browser_pool is None:
            raise RuntimeError(
                "agentic planner requires a browser_pool; configure one on AppDependencies"
            )
        cm = self.browser_pool.checkout()
        sess = await cm.__aenter__()
        self._cm = cm
        self._session = sess
        try:
            yield self
        finally:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                logger.exception("planning browser close failed")
            self._session = None
            self._cm = None

    # ---------- tools -----------------------------------------------------

    async def navigate(self, url: str) -> dict[str, Any]:
        if self._session is None:
            return {"error": "no planning session"}
        logger.info("agentic.navigate url=%s", url)
        try:
            await self._session.navigate(url)
        except Exception as e:  # noqa: BLE001
            logger.warning("agentic.navigate failed url=%s: %s", url, e)
            return {"error": f"navigate failed: {type(e).__name__}: {e}"}
        meta = await self._page_meta()
        return {"ok": True, **meta}

    async def inspect_page(self) -> dict[str, Any]:
        if self._session is None:
            return {"error": "no planning session"}
        logger.debug("agentic.inspect_page")
        try:
            result = await self._session.evaluate(_INSPECT_JS)
        except Exception as e:  # noqa: BLE001
            logger.warning("agentic.inspect_page failed: %s", e)
            return {"error": f"inspect failed: {type(e).__name__}: {e}"}
        if not isinstance(result, dict):
            return {"error": "inspect returned non-object"}
        return result

    async def login_with_grant(
        self, *, login_url: str, account_alias: str
    ) -> dict[str, Any]:
        """Log in using the tenant's cap.web_login grant. Selectors are
        auto-discovered. Refuses if the grant doesn't exist."""
        from aakaar.capabilities.web_login import CAP_REF as WEB_LOGIN_REF

        logger.info("agentic.login_with_grant url=%s alias=%s", login_url, account_alias)
        # Build a synthetic ActivityContext — fetch_credentials only needs
        # tenant_id, vault, granted_capabilities. The other fields are unused.
        try:
            creds = _fetch_creds_for_planning(
                vault=self.vault,
                tenant_id=self.tenant_id,
                granted_capabilities=self.granted_capabilities,
                capability_ref=WEB_LOGIN_REF,
                account_alias=account_alias,
            )
        except PermissionError as e:
            logger.warning("agentic.login_with_grant: grant lookup failed alias=%s: %s", account_alias, e)
            return {"error": f"grant lookup failed: {e}"}

        if self._session is None:
            return {"error": "no planning session"}
        try:
            await self._session.navigate(login_url)
            descriptor = await discover_login_form(self._session)
            if not descriptor.password_selector:
                return {
                    "error": "could not find a login form on the page",
                    "ambiguity_reasons": descriptor.ambiguity_reasons,
                }
            if descriptor.captcha_kind:
                return {
                    "error": (
                        f"page has a {descriptor.captcha_kind} challenge; "
                        "plan-time login can't solve captchas. Compose a "
                        "DAG with cap.web_login (it pauses HITL at run "
                        "time) instead of trying to log in here."
                    ),
                    "captcha_kind": descriptor.captcha_kind,
                }
            await self._session.wait_for(
                descriptor.username_selector, timeout_ms=self.timeout_ms
            )
            await self._session.fill(descriptor.username_selector, creds["username"])
            await self._session.fill(descriptor.password_selector, creds["password"])
            await self._session.click(descriptor.submit_selector)
            # Best-effort: wait for the username field to disappear.
            with suppress(Exception):
                await self._session.wait_for(
                    descriptor.username_selector, timeout_ms=self.timeout_ms
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("agentic.login_with_grant failed url=%s: %s", login_url, e)
            return {"error": f"login failed: {type(e).__name__}: {e}"}
        meta = await self._page_meta()
        logger.info("agentic.login_with_grant ok landed_url=%s", meta.get("url"))
        return {"ok": True, "logged_in": True, **meta}

    async def _page_meta(self) -> dict[str, Any]:
        if self._session is None:
            return {}
        try:
            result = await self._session.evaluate(
                "({url: location.href, title: document.title})"
            )
        except Exception:  # noqa: BLE001
            return {}
        # Test fakes return None when no programmed response matches.
        return result if isinstance(result, dict) else {}


def _fetch_creds_for_planning(
    *,
    vault: Vault,
    tenant_id: UUID,
    granted_capabilities: dict[str, dict[str, Any]],
    capability_ref: str,
    account_alias: str,
) -> dict[str, str]:
    """Inline copy of `fetch_credentials` shaped for plan-time use (no
    full ActivityContext). Returns the secret bundle, raises
    PermissionError on missing grant or empty vault entry."""
    # We use the real helper indirectly by constructing a minimal stand-in.
    # That keeps the auth/security logic in one place.
    from types import SimpleNamespace

    stub = SimpleNamespace(
        tenant_id=tenant_id,
        vault=vault,
        granted_capabilities=granted_capabilities,
    )
    return fetch_credentials(
        stub,  # type: ignore[arg-type]
        capability_ref=capability_ref,
        account_alias=account_alias,
    )
