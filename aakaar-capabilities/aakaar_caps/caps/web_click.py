"""cap.web_click — click a button/link by image, text, or CSS selector.

Why this exists (and why ``browser.click_by_text`` is not enough): widget
frameworks like ZK/ZKoss render icon-only controls as an ``<img>`` (or a CSS
background image) wrapped in a clickable ``<a class="z-toolbarbutton">`` /
``<button>`` with **no visible text, an empty ``alt``, and ``aria-hidden``** on
the image. The CTS Outward "Logout" control is exactly this shape::

    <a id="e1EF9" class="z-toolbarbutton" role="button">
      <span class="z-toolbarbutton-content">
        <img src="/outward/images/logout.png" alt="" aria-hidden="true">
      </span>
    </a>

``browser.click_by_text("logout")`` finds nothing — there is no "logout" text
anywhere. This capability resolves the control by the *image asset name*
(``image="logout"`` → any ``<img>`` whose ``src``/``alt``/``title`` contains
"logout"), then clicks the **nearest clickable ancestor** so the framework's
delegated click handler fires. It also accepts ``text`` (visible text / title /
aria-label, climbing to the clickable ancestor) and an explicit ``selector``,
so a single node covers every click shape in a flow.

Shared capability: the SAME code runs on the server and on a remote agent. It
programs only against the portable CapabilityContext (the browser session via
``session_state``); element resolution runs in-page via ``session.evaluate`` so
it works identically against Playwright on either host.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aakaar_caps.browser.state import get_session
from aakaar_caps.caps._zkutil import safe_evaluate
from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.web_click"

_DEFAULT_TIMEOUT_MS = 15000
_POLL_INTERVAL_S = 0.25


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str = Field(
        description=(
            "Browser session handle from an upstream node "
            "(e.g. ${login_ctsoutward.session})."
        )
    )
    image: str | None = Field(
        default=None,
        description=(
            "Substring matched (case-insensitive) against an image button's "
            "src / alt / title / aria-label, e.g. 'logout' for an "
            "<img src='/outward/images/logout.png'>. Clicks the nearest "
            "clickable ancestor (<a>/<button>/[role=button]/.z-toolbarbutton), "
            "so it handles ZK icon buttons that have no visible text. Use this "
            "for image/icon-only controls such as Logout, Save, Search, Print."
        ),
    )
    text: str | None = Field(
        default=None,
        description=(
            "Visible text, title, or aria-label of the control to click. Like "
            "browser.click_by_text but also matches title/aria-label and clicks "
            "the nearest clickable ancestor. Use for normal labelled buttons "
            "and links (e.g. 'OK', 'Reports')."
        ),
    )
    selector: str | None = Field(
        default=None,
        description="Explicit CSS selector to click (takes priority over image/text).",
    )
    timeout_ms: int = Field(
        default=_DEFAULT_TIMEOUT_MS,
        ge=1000,
        le=120000,
        description="How long to wait for the target to appear before failing.",
    )

    @model_validator(mode="after")
    def _exactly_one_target(self) -> _Inputs:
        provided = [v for v in (self.selector, self.image, self.text) if v]
        if not provided:
            raise ValueError(
                "cap.web_click needs one of: 'image', 'text', or 'selector'."
            )
        return self


class _Outputs(BaseModel):
    clicked: bool = Field(description="True when a control was found and clicked.")
    matched_by: str = Field(
        description="Which strategy matched: 'selector' | 'image' | 'image_input' "
        "| 'image_bg' | 'text' | 'text_any'."
    )
    selector: str = Field(description="CSS selector of the element that was clicked.")


SPEC = CapabilitySpec(
    ref=CAP_REF,
    description=(
        "Click a button or link identified by an image/icon asset name, by "
        "visible text, or by an explicit CSS selector. Resolves icon-only "
        "controls (image with no text — e.g. a ZK toolbar Logout button) by "
        "their image src/alt/title and clicks the nearest clickable ancestor. "
        "Operates on an existing browser session; chain after cap.web_login / "
        "cap.open_url. Provide exactly one of `image`, `text`, or `selector`."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("browser", "click"),
    side_effecting=True,
)


# JS resolver. Returns {ok, selector, matched_by, tag, id} for the element to
# click, or {ok:false}. Element resolution (incl. climbing to the clickable
# ancestor) happens in-page so it is identical on server and agent. `{params}`
# is JSON-injected by the handler — safe, it becomes a JS object literal.
_RESOLVE_JS = r"""
(() => {
  const P = __PARAMS__;
  const needle = (P.value || "").toLowerCase();

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
  // Build a document-unique CSS selector (prefer id, then name/data-testid,
  // then a :nth-child path). Mirrors the login-form discovery strategy.
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

  const CLICKABLE =
    "a, button, [role='button'], [onclick], input[type='button'], " +
    "input[type='submit'], input[type='image'], .z-button, .z-toolbarbutton, " +
    ".z-menuitem, .z-tab, .z-treerow, .z-listitem";
  function clickableAncestor(el) {
    if (!el) return null;
    const c = el.closest && el.closest(CLICKABLE);
    return c || el;
  }

  let target = null;
  let matchedBy = null;

  if (P.mode === "selector") {
    let el = null;
    try { el = document.querySelector(P.value); } catch (e) { el = null; }
    if (el && visible(el)) { target = el; matchedBy = "selector"; }
  } else if (P.mode === "image") {
    const imgs = Array.from(document.querySelectorAll("img")).filter(visible);
    const img = imgs.find((im) => {
      const hay = (
        (im.getAttribute("src") || "") + " " +
        (im.getAttribute("alt") || "") + " " +
        (im.getAttribute("title") || "") + " " +
        (im.getAttribute("aria-label") || "")
      ).toLowerCase();
      return hay.includes(needle);
    });
    if (img) { target = clickableAncestor(img); matchedBy = "image"; }
    if (!target) {
      const inp = Array.from(document.querySelectorAll("input[type='image']"))
        .filter(visible)
        .find((i) => ((i.getAttribute("src") || "") + " " + (i.getAttribute("alt") || "")).toLowerCase().includes(needle));
      if (inp) { target = clickableAncestor(inp); matchedBy = "image_input"; }
    }
    if (!target) {
      const all = Array.from(
        document.querySelectorAll("a, button, span, div, i, [role='button'], .z-toolbarbutton")
      ).filter(visible);
      const bg = all.find((el) => ((window.getComputedStyle(el).backgroundImage) || "").toLowerCase().includes(needle));
      if (bg) { target = clickableAncestor(bg); matchedBy = "image_bg"; }
    }
  } else if (P.mode === "text") {
    const all = Array.from(document.querySelectorAll(CLICKABLE)).filter(visible);
    let best = null, bestScore = 0;
    for (const el of all) {
      const t = (el.textContent || "").trim().toLowerCase();
      const ti = (el.getAttribute("title") || "").toLowerCase();
      const ar = (el.getAttribute("aria-label") || "").toLowerCase();
      let s = 0;
      if (t === needle || ti === needle || ar === needle) s = 3;
      else if (t.includes(needle) || ti.includes(needle) || ar.includes(needle)) s = 1;
      if (s > bestScore) { bestScore = s; best = el; }
    }
    if (best) { target = best; matchedBy = "text"; }
    if (!target) {
      const any = Array.from(document.querySelectorAll("*"))
        .filter(visible)
        .find((el) => (el.textContent || "").trim().toLowerCase() === needle);
      if (any) { target = clickableAncestor(any); matchedBy = "text_any"; }
    }
  }

  if (!target) return { ok: false };
  try { target.scrollIntoView({ block: "center", inline: "center" }); } catch (e) {}
  return {
    ok: true,
    selector: bestSelector(target),
    matched_by: matchedBy,
    tag: target.tagName.toLowerCase(),
    id: target.id || null,
  };
})()
"""

# JS fallback: re-find the element by the resolved selector and dispatch a
# native click. ZK binds delegated handlers, so el.click() triggers logout the
# same as a real click — used only if Playwright's actionable click raises.
_JS_CLICK = r"""
(() => {
  let el = null;
  try { el = document.querySelector(__SELECTOR__); } catch (e) { el = null; }
  if (!el) return false;
  el.click();
  return true;
})()
"""


def _resolve_js(mode: str, value: str) -> str:
    return _RESOLVE_JS.replace("__PARAMS__", json.dumps({"mode": mode, "value": value}))


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = get_session(ctx.session_state, inputs["session"])

    # Priority: explicit selector > image > text.
    if inputs.get("selector"):
        mode, value = "selector", str(inputs["selector"])
    elif inputs.get("image"):
        mode, value = "image", str(inputs["image"])
    elif inputs.get("text"):
        mode, value = "text", str(inputs["text"])
    else:  # pragma: no cover - guarded by the input model_validator
        raise ValueError("cap.web_click needs one of: 'image', 'text', or 'selector'.")

    timeout_ms = int(inputs.get("timeout_ms", _DEFAULT_TIMEOUT_MS))
    logger.info(
        "cap.web_click start run_id=%s session=%s by=%s value=%r",
        ctx.run_id, inputs["session"], mode, value,
    )

    # Poll the in-page resolver until the target appears or we hit the deadline
    # (ZK pages render via async updates after navigation).
    js = _resolve_js(mode, value)
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    resolved: dict[str, Any] | None = None
    while True:
        result = await safe_evaluate(sess, js)
        if isinstance(result, dict) and result.get("ok"):
            resolved = result
            break
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(_POLL_INTERVAL_S)

    if resolved is None:
        raise RuntimeError(
            f"cap.web_click: no clickable control matched {mode}={value!r} "
            f"within {timeout_ms}ms"
        )

    selector = str(resolved["selector"])
    matched_by = str(resolved.get("matched_by") or mode)

    # Primary: Playwright actionable click (real mouse events, scroll-into-view,
    # visibility/stability checks). Fallback: native el.click() in-page, which
    # ZK's delegated handlers honour, if the actionable click can't land.
    try:
        await sess.click(selector)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "cap.web_click: actionable click on %r failed (%s); falling back to JS click",
            selector, exc,
        )
        ok = await safe_evaluate(sess, _JS_CLICK.replace("__SELECTOR__", json.dumps(selector)))
        if not ok:
            raise RuntimeError(
                f"cap.web_click: matched {selector!r} but could not click it"
            ) from exc

    logger.info(
        "cap.web_click ok run_id=%s selector=%s matched_by=%s",
        ctx.run_id, selector, matched_by,
    )
    return {"clicked": True, "matched_by": matched_by, "selector": selector}
