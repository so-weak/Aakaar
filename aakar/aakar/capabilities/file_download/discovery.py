"""Post-login download-target discovery.

cap.file_download accepts an optional `target_hint` — a natural-language
description of what to download. The handler runs the JS below to
collect every visible "downloadable-looking" element on the page (links,
buttons, table-row triggers), then fuzzy-matches the hint against their
visible text + accessible labels in Python.

Why two layers (JS + Python) rather than doing it all in JS:
  - The JS pass needs to walk the DOM. That's cheap and stable.
  - Fuzzy matching with em-dash normalization, abbreviated month names,
    bag-of-words scoring etc. is easier in Python and easier to test.

Selectors returned must be valid CSS evaluable at the document root
(Playwright `page.click` runs them there). The same `bestSelector`
helper as the login form discovery applies.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


# Single JS payload run via `session.evaluate`. Returns a list of
# candidates the Python side will rank.
DISCOVERY_JS = r"""
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
  function uniqueOnPage(s) {
    try { return document.querySelectorAll(s).length === 1; } catch (e) { return false; }
  }
  function bestSelector(el) {
    if (!el || !(el instanceof Element)) return null;
    if (el.id) return "#" + cssEscape(el.id);
    const name = el.getAttribute && el.getAttribute("name");
    if (name) {
      const s = el.tagName.toLowerCase() + "[name=" + JSON.stringify(name) + "]";
      if (uniqueOnPage(s)) return s;
    }
    const dataTestId = el.getAttribute && el.getAttribute("data-testid");
    if (dataTestId) return "[data-testid=" + JSON.stringify(dataTestId) + "]";
    const href = el.getAttribute && el.getAttribute("href");
    if (href && el.tagName === "A") {
      const s = "a[href=" + JSON.stringify(href) + "]";
      if (uniqueOnPage(s)) return s;
    }
    const cls = (el.className || "").split(/\s+/).filter(Boolean).slice(0, 2).join(".");
    if (cls) {
      const s = el.tagName.toLowerCase() + "." + cls.split(".").map(cssEscape).join(".");
      if (uniqueOnPage(s)) return s;
    }
    // Fall back to a parent-anchored :nth-child path. Strict CSS, valid
    // at the document root.
    const path = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur.tagName !== "BODY" && cur.tagName !== "HTML") {
      let step = cur.tagName.toLowerCase();
      if (cur.id) {
        path.unshift("#" + cssEscape(cur.id));
        return path.join(" > ");
      }
      const parent = cur.parentElement;
      if (parent) {
        const idx = Array.from(parent.children).indexOf(cur) + 1;
        step += ":nth-child(" + idx + ")";
      }
      path.unshift(step);
      cur = cur.parentElement;
    }
    return path.length ? path.join(" > ") : el.tagName.toLowerCase();
  }
  function ariaLabelOf(el) {
    return (el.getAttribute && el.getAttribute("aria-label")) || null;
  }
  function visibleTextOf(el) {
    return ((el.innerText || el.textContent || "").trim()).slice(0, 200);
  }
  function rowContextOf(el) {
    const tr = el.closest && el.closest("tr,li,article");
    if (!tr) return null;
    return ((tr.innerText || tr.textContent || "").trim()).slice(0, 400);
  }

  // Candidate set: visible <a>, <button>, [role=button], [role=link],
  // input[type=submit/button]. We deliberately exclude pure form
  // submission inputs that have no text/label.
  const sel =
    "a, button, [role='button'], [role='link'], input[type='submit'], input[type='button']";
  const els = Array.from(document.querySelectorAll(sel)).filter(visible);
  const seen = new Set();
  const out = [];
  for (const el of els) {
    const text = visibleTextOf(el);
    const aria = ariaLabelOf(el);
    if (!text && !aria) continue;
    const selector = bestSelector(el);
    if (!selector || seen.has(selector)) continue;
    seen.add(selector);
    out.push({
      selector,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute("role") || el.tagName.toLowerCase(),
      text,
      aria_label: aria,
      href: el.getAttribute("href") || null,
      // Surrounding row context — useful when the link is just "Download"
      // and the report name lives in a sibling cell.
      row_context: rowContextOf(el),
    });
    if (out.length >= 200) break;
  }
  return { candidates: out, count_total: els.length };
})()
"""


@dataclass(slots=True)
class Candidate:
    """One downloadable-looking element on the page."""

    selector: str
    tag: str
    role: str
    text: str
    aria_label: str | None
    href: str | None
    row_context: str | None
    score: float = 0.0
    """Set by `rank_candidates`."""


# ---------- text normalization + scoring -------------------------------------


_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_DASH_RE = re.compile(r"[—–\-]+")


_MONTH_ALIASES = {
    "jan": "january", "feb": "february", "mar": "march", "apr": "april",
    "jun": "june", "jul": "july", "aug": "august", "sep": "september",
    "sept": "september", "oct": "october", "nov": "november", "dec": "december",
}


def _normalize(text: str) -> str:
    """Lowercase, strip accents, normalize dashes/whitespace, drop punctuation,
    expand month abbreviations. The result is used for substring + token
    overlap matching."""
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = _DASH_RE.sub(" ", s)
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    # Expand month abbreviations after punctuation/dash strip so "may'26"
    # becomes "may 26" first.
    parts = []
    for tok in s.split():
        parts.append(_MONTH_ALIASES.get(tok, tok))
    return " ".join(parts)


def _tokens(text: str) -> set[str]:
    return {t for t in _normalize(text).split() if len(t) > 1}


# Generic clickable labels that, on their own, almost never tell us *which*
# row/card a click belongs to. When a candidate's visible text is one of
# these AND we have row_context to lean on, we drop the standalone-text
# score from the candidate's `max(...)` so the row context drives the
# match. This stops something like a "Transactions" filter pill from
# out-scoring a real download button inside a card titled "Biller
# Transactions — May 2026". Aria-labels are NOT touched — if a site
# author bothered to set `aria-label="Download Biller Transactions May
# 2026"`, that's a high-signal label and we still score against it.
_GENERIC_CLICK_TEXTS: frozenset[str] = frozenset(
    {
        "download",
        "download csv",
        "download xlsx",
        "download xls",
        "download pdf",
        "download file",
        "view",
        "open",
        "select",
        "go",
        "submit",
        "click here",
        "more",
        "details",
    }
)


def _score(hint_norm: str, hint_tokens: set[str], text: str) -> float:
    """0.0–1.0. Highest when the candidate text contains the full hint as
    a substring; falls off as token overlap drops."""
    if not text:
        return 0.0
    cand_norm = _normalize(text)
    if not cand_norm:
        return 0.0
    if hint_norm and hint_norm in cand_norm:
        return 1.0
    cand_tokens = _tokens(text)
    if not hint_tokens or not cand_tokens:
        return 0.0
    common = hint_tokens & cand_tokens
    if not common:
        return 0.0
    # Jaccard-ish but biased toward hint coverage (we care more that the
    # candidate covers what the user asked for than the other way around).
    coverage = len(common) / len(hint_tokens)
    precision = len(common) / len(cand_tokens)
    return 0.7 * coverage + 0.3 * precision


def rank_candidates(
    raw: list[dict[str, object]] | None,
    *,
    target_hint: str,
) -> list[Candidate]:
    """Return candidates sorted by descending score. Raw items may have
    missing fields; we coerce defensively. Score uses text + aria_label +
    row_context (so e.g. a "Download" button inside a row labeled
    "Biller Transactions — May 2026" still scores high)."""
    if not raw:
        return []
    hint_norm = _normalize(target_hint)
    hint_tokens = _tokens(target_hint)
    out: list[Candidate] = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        c = Candidate(
            selector=str(r.get("selector") or ""),
            tag=str(r.get("tag") or ""),
            role=str(r.get("role") or ""),
            text=str(r.get("text") or ""),
            aria_label=(str(r["aria_label"]) if r.get("aria_label") else None),
            href=(str(r["href"]) if r.get("href") else None),
            row_context=(str(r["row_context"]) if r.get("row_context") else None),
        )
        if not c.selector:
            continue
        # Score the best of the candidate's text fields.
        text_is_generic = (
            c.row_context is not None
            and _normalize(c.text) in _GENERIC_CLICK_TEXTS
        )
        scores: list[float] = []
        if not text_is_generic:
            # When the button's own text is uninformative (e.g. literal
            # "Download CSV") and we have row context, drop it from the
            # max — otherwise an exact-token filter pill elsewhere on the
            # page out-scores the right button.
            scores.append(_score(hint_norm, hint_tokens, c.text))
        if c.aria_label:
            scores.append(_score(hint_norm, hint_tokens, c.aria_label))
        if c.row_context:
            scores.append(_score(hint_norm, hint_tokens, c.row_context))
        if not scores:
            # Defensive — discovery always provides at least `text`, but
            # if a row-only generic candidate somehow surfaced with no
            # other fields, score it at zero rather than crash.
            scores.append(0.0)
        c.score = max(scores)
        out.append(c)
    out.sort(key=lambda x: x.score, reverse=True)
    return out


# ---------- decision policy --------------------------------------------------


@dataclass(slots=True)
class Pick:
    """The handler's decision after ranking."""

    chosen: Candidate | None
    """Set when we have a clear winner."""
    ambiguous: list[Candidate]
    """Set when the top candidates are too close to call. Caller surfaces
    these to a human for tiebreak."""
    none_match: bool = False


# Score thresholds. Picked empirically against real-site fixtures.
#  - HIGH-CONFIDENCE path: top.score >= 0.85 AND top - runner >= 0.20
#    (typical of a substring-hit row_context match).
#  - DECISIVE-MARGIN path: top.score >= 0.55 AND top - runner >= 0.20
#    (covers the common case where partial token-overlap puts the top
#    candidate around 0.60-0.75 — well below the substring threshold
#    but so far ahead of the runner-up that asking the user would just
#    be padding the funnel).
#  - top < 0.35 → no candidate matches, surface as missing.
#  - everything else → ambiguous (hand to user).
_CLEAR_TOP = 0.85
_CLEAR_MARGIN = 0.20
_DECISIVE_TOP = 0.55
_DECISIVE_MARGIN = 0.20
_NO_MATCH_TOP = 0.35


def decide(ranked: list[Candidate]) -> Pick:
    if not ranked:
        return Pick(chosen=None, ambiguous=[], none_match=True)
    top = ranked[0]
    if top.score < _NO_MATCH_TOP:
        return Pick(chosen=None, ambiguous=[], none_match=True)
    runner = ranked[1].score if len(ranked) > 1 else 0.0
    margin = top.score - runner
    # High-confidence path: substring-hit or aria-hit puts the top near 1.0.
    if top.score >= _CLEAR_TOP and margin >= _CLEAR_MARGIN:
        return Pick(chosen=top, ambiguous=[])
    # Decisive-margin path: top is meaningfully ahead, even if its
    # absolute score reflects only partial token overlap. We require a
    # 0.55 floor so we don't auto-pick a barely-matching candidate just
    # because the rest of the page scored zero.
    if top.score >= _DECISIVE_TOP and margin >= _DECISIVE_MARGIN:
        return Pick(chosen=top, ambiguous=[])
    # Ambiguous: surface up to 5 contenders. We include anything within
    # 0.15 of the top score, capped at 5, so the user has a small list
    # rather than a wall of links.
    cutoff = max(_NO_MATCH_TOP, top.score - 0.15)
    contenders = [c for c in ranked if c.score >= cutoff][:5]
    return Pick(chosen=None, ambiguous=contenders)
