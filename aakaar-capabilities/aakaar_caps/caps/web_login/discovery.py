"""Login-form auto-discovery.

The JS payload below runs in the page after navigation and returns a
descriptor of the login form's elements + any captcha widgets. It uses
the password input as the anchor — every login page has one, and forms
without one aren't login pages — and walks outward from there.

The walk does not assume a ``<form>`` element. Widget frameworks such as
ZK/ZKoss render form-less login screens where the username, password and
submit live in sibling grid cells. For those, discovery climbs from the
password to the smallest ancestor that also holds the username input (the
login panel) and searches that, so the three controls are still resolved.

Returned shape::

    {
        "ok": true,
        "ambiguity_reasons": ["multiple_password_inputs", ...],
        "username_selector": "input#email",
        "password_selector": "input[name='password']",
        "submit_selector": "button[type='submit']",
        "captcha_image_selector": "img.captcha" | null,
        "captcha_input_selector": "input[name='captcha']" | null,
        "captcha_kind": "image" | "recaptcha" | "hcaptcha" | "turnstile" | null,
        "form_outer_html_excerpt": "<form>...</form>",   // for LLM fallback
    }

Selector strategy: the JS prefers stable ids, then `name=` attributes,
then class+tag, then a positional path within the form. It never relies
on raw text content — translatable strings change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class LoginFormDescriptor:
    ok: bool
    ambiguity_reasons: list[str]
    username_selector: str | None
    password_selector: str | None
    submit_selector: str | None
    captcha_image_selector: str | None
    captcha_input_selector: str | None
    captcha_kind: str | None
    """One of: 'image', 'recaptcha', 'hcaptcha', 'turnstile', None."""
    form_outer_html_excerpt: str
    """First ~4 KB of the form's outerHTML, stripped of scripts and most
    long attributes. Used as the prompt context if we fall back to the
    LLM for tiebreaking."""

    @classmethod
    def from_js_result(cls, result: Any) -> LoginFormDescriptor:
        if not isinstance(result, dict):
            return cls(
                ok=False,
                ambiguity_reasons=["js_returned_non_object"],
                username_selector=None,
                password_selector=None,
                submit_selector=None,
                captcha_image_selector=None,
                captcha_input_selector=None,
                captcha_kind=None,
                form_outer_html_excerpt="",
            )
        return cls(
            ok=bool(result.get("ok", False)),
            ambiguity_reasons=list(result.get("ambiguity_reasons") or []),
            username_selector=result.get("username_selector"),
            password_selector=result.get("password_selector"),
            submit_selector=result.get("submit_selector"),
            captcha_image_selector=result.get("captcha_image_selector"),
            captcha_input_selector=result.get("captcha_input_selector"),
            captcha_kind=result.get("captcha_kind"),
            form_outer_html_excerpt=str(result.get("form_outer_html_excerpt") or ""),
        )

    @property
    def has_captcha(self) -> bool:
        return self.captcha_kind is not None


# JS payload. Wrapped in `() => { ... }()` so `await session.evaluate(JS)`
# returns the result directly (Playwright `page.evaluate(string)` evaluates
# expressions, not statements; an IIFE is the canonical workaround).
DISCOVERY_JS = r"""
(() => {
  const reasons = [];

  function cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return s.replace(/([^a-zA-Z0-9_-])/g, "\\$1");
  }

  function uniqueOnPage(selector) {
    try {
      return document.querySelectorAll(selector).length === 1;
    } catch (e) {
      return false;
    }
  }

  function bestSelector(el) {
    // Goal: return a CSS selector that uniquely matches `el` on the
    // current page. Anything we return must work with
    // `document.querySelector(...)` in plain CSS (no Playwright extensions
    // like :has-text()) — Playwright's page.fill / wait_for run the
    // selector at the document root, so a selector that's only "unique
    // within this form" needs the form prefix baked in.
    if (!el || !(el instanceof Element)) return null;
    if (el.id) return "#" + cssEscape(el.id);
    const name = el.getAttribute && el.getAttribute("name");
    if (name) {
      const s = el.tagName.toLowerCase() + "[name=" + JSON.stringify(name) + "]";
      if (uniqueOnPage(s)) return s;
    }
    const dataTestId = el.getAttribute && el.getAttribute("data-testid");
    if (dataTestId) return "[data-testid=" + JSON.stringify(dataTestId) + "]";
    // Try attribute-anchored selectors that tend to be unique on a login
    // page even when ids / names are missing: input[type='password'],
    // input[type='email'], etc.
    if (el.tagName === "INPUT") {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      const typed = "input[type=" + JSON.stringify(type) + "]";
      if (uniqueOnPage(typed)) return typed;
      const ph = el.getAttribute("placeholder");
      if (ph) {
        const s = "input[placeholder=" + JSON.stringify(ph) + "]";
        if (uniqueOnPage(s)) return s;
      }
      const al = el.getAttribute("aria-label");
      if (al) {
        const s = "input[aria-label=" + JSON.stringify(al) + "]";
        if (uniqueOnPage(s)) return s;
      }
      const auto = el.getAttribute("autocomplete");
      if (auto) {
        const s = "input[autocomplete=" + JSON.stringify(auto) + "]";
        if (uniqueOnPage(s)) return s;
      }
    }
    const cls = (el.className || "").split(/\s+/).filter(Boolean).slice(0, 2).join(".");
    if (cls) {
      const s = el.tagName.toLowerCase() + "." + cls.split(".").map(cssEscape).join(".");
      if (uniqueOnPage(s)) return s;
    }
    // Last resort: build a parent-anchored path. We walk up to the
    // nearest ancestor with a stable handle (id / form / body) and
    // append :nth-child indices for each step. This produces a strictly
    // valid CSS selector — :nth-child works at the document root unlike
    // the bare :nth-of-type fallback we used before.
    const path = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur.tagName !== "BODY" && cur.tagName !== "HTML") {
      let step = cur.tagName.toLowerCase();
      if (cur.id) {
        path.unshift("#" + cssEscape(cur.id));
        return path.join(" > ");
      }
      if (cur.tagName === "FORM") {
        // Anchor the path at "form"; if the form has a name use it.
        const fname = cur.getAttribute("name");
        path.unshift(fname ? "form[name=" + JSON.stringify(fname) + "]" : "form");
        const candidate = path.join(" > ");
        if (uniqueOnPage(candidate)) return candidate;
        break;
      }
      const parent = cur.parentElement;
      if (parent) {
        const idx = Array.from(parent.children).indexOf(cur) + 1;
        step += ":nth-child(" + idx + ")";
      }
      path.unshift(step);
      cur = cur.parentElement;
    }
    const tag = el.tagName.toLowerCase();
    return path.length ? path.join(" > ") : tag;
  }

  function visible(el) {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  // 1. Anchor on a visible password input.
  const allPasswords = Array.from(document.querySelectorAll("input[type='password']")).filter(visible);
  if (allPasswords.length === 0) {
    return { ok: false, ambiguity_reasons: ["no_password_input"], form_outer_html_excerpt: "" };
  }
  if (allPasswords.length > 1) reasons.push("multiple_password_inputs");
  const password = allPasswords[0];

  // Search scope. A login form is usually wrapped in a <form>, but widget
  // frameworks (ZK/ZKoss, and some SPA grids) render a form-LESS layout where
  // the username, password and submit live in sibling grid cells with NO
  // enclosing <form>. Anchoring the search on `password.parentElement` there
  // collapses the scope to the single cell that holds only the password, so
  // the username + submit (in sibling cells) are never found. Instead, when
  // there's no <form>, walk up from the password to the smallest ancestor that
  // also contains a visible non-password text input (the username) — that's
  // the login panel. The walk is depth-capped so we never grab the whole page,
  // and falls back to the password's own parent for password-only flows.
  const form = password.closest("form");
  let scope = form;
  if (!scope) {
    let cur = password.parentElement;
    for (let depth = 0; cur && depth < 10 && cur.tagName !== "BODY" && cur.tagName !== "HTML"; depth++) {
      const hasOtherText = Array.from(
        cur.querySelectorAll(
          "input[type='text'], input[type='email'], input[type='tel'], input:not([type])"
        )
      ).filter(visible).some((el) => el !== password);
      if (hasOtherText) { scope = cur; break; }
      cur = cur.parentElement;
    }
    if (!scope) scope = password.parentElement || document.body;
  }

  // 2. Username = the visible text/email/tel input that precedes the password
  // in tab order. We approximate "tab order" with DOM order within the scope.
  const candidates = Array.from(
    scope.querySelectorAll(
      "input[type='text'], input[type='email'], input[type='tel'], input:not([type])"
    )
  ).filter(visible);
  let username = null;
  for (const c of candidates) {
    if (c.compareDocumentPosition(password) & Node.DOCUMENT_POSITION_FOLLOWING) {
      username = c; // c precedes password
    }
  }
  if (!username && candidates.length === 1) username = candidates[0];
  if (!username) reasons.push("no_username_input_found");
  if (candidates.length > 1 && candidates.indexOf(username) >= 0 && candidates.length > 2) {
    reasons.push("multiple_text_inputs_before_password");
  }

  // 3. Submit control — within the scope, prefer a real submit; then a plain
  // <button>; then widget-framework buttons. ZK/ZKoss renders the login button
  // as <button class="z-button"> or <a class="z-button">, and other libraries
  // use role="button" on non-button elements. Text is never matched —
  // translatable labels change between locales.
  let submit =
    scope.querySelector("button[type='submit']") ||
    scope.querySelector("input[type='submit']") ||
    scope.querySelector("button:not([type])") ||
    scope.querySelector("button") ||
    scope.querySelector("a.z-button, .z-button, [role='button']");
  if (!submit) {
    submit = document.querySelector("button[type='submit'], input[type='submit']");
  }
  if (!submit) reasons.push("no_submit_button_found");

  // 4. Captcha detection.
  let captchaImage = null;
  let captchaInput = null;
  let captchaKind = null;
  // 4a. classic <img> captcha + a sibling input.
  const captchaImgEl = scope.querySelector(
    "img[alt*='captcha' i], img[src*='captcha' i], img[name*='captcha' i], img.captcha, img#captcha"
  );
  if (captchaImgEl) {
    captchaImage = captchaImgEl;
    captchaKind = "image";
    const ci =
      scope.querySelector(
        "input[name*='captcha' i], input[id*='captcha' i], input[placeholder*='captcha' i]"
      ) || null;
    if (ci) captchaInput = ci;
    else reasons.push("captcha_image_without_input");
  } else {
    // 4b. recaptcha / hcaptcha / turnstile — surfaced as iframes or div widgets.
    if (
      document.querySelector("iframe[src*='recaptcha']") ||
      document.querySelector(".g-recaptcha") ||
      document.querySelector("[data-sitekey][data-callback]")
    ) {
      captchaKind = "recaptcha";
    } else if (document.querySelector("iframe[src*='hcaptcha']") || document.querySelector(".h-captcha")) {
      captchaKind = "hcaptcha";
    } else if (
      document.querySelector("iframe[src*='challenges.cloudflare.com']") ||
      document.querySelector(".cf-turnstile")
    ) {
      captchaKind = "turnstile";
    }
    if (captchaKind && captchaKind !== "image") {
      reasons.push("third_party_captcha_" + captchaKind);
    }
  }

  // 5. Snapshot of the scope's outerHTML for LLM fallback. For a form-less
  //    page this is the login panel we walked up to, NOT just the password's
  //    cell — so the username + submit are included for the LLM to see. Strip
  //    <script> and trim long attributes; cap at 4 KB.
  let snapshot = "";
  if (scope && scope.cloneNode) {
    const clone = scope.cloneNode(true);
    clone.querySelectorAll("script,style").forEach((n) => n.remove());
    snapshot = clone.outerHTML || "";
    if (snapshot.length > 4096) snapshot = snapshot.slice(0, 4096) + "…";
  }

  return {
    ok: !!(username && password && submit) || allPasswords.length === 1,
    ambiguity_reasons: reasons,
    username_selector: bestSelector(username),
    password_selector: bestSelector(password),
    submit_selector: bestSelector(submit),
    captcha_image_selector: bestSelector(captchaImage),
    captcha_input_selector: bestSelector(captchaInput),
    captcha_kind: captchaKind,
    form_outer_html_excerpt: snapshot,
  };
})()
"""


async def discover_login_form(session: object) -> LoginFormDescriptor:
    """Run the discovery JS in the page and return a typed descriptor.

    `session` must be a `BrowserSession` (duck-typed as having
    `evaluate(js)`). Errors during evaluation surface as a non-ok
    descriptor — callers decide whether to fall back to the LLM.
    """
    try:
        result = await session.evaluate(DISCOVERY_JS)  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        return LoginFormDescriptor(
            ok=False,
            ambiguity_reasons=[f"evaluate_error:{type(e).__name__}"],
            username_selector=None,
            password_selector=None,
            submit_selector=None,
            captcha_image_selector=None,
            captcha_input_selector=None,
            captcha_kind=None,
            form_outer_html_excerpt="",
        )
    return LoginFormDescriptor.from_js_result(result)
