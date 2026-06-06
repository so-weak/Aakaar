"""cap.web_scrape — read a web page and return its content (optionally structured).

Two entry modes, mutually exclusive at the "where do I get the page" step:

  - `url`: open a fresh browser session, navigate to the URL, scrape, and
    tear the session down before returning. Use this for the common
    one-shot "scrape this public page" intent.
  - `session_id`: reuse a session opened upstream (cap.open_url /
    cap.web_login / browser.open_session). The session is left open so
    downstream nodes can keep using it — this capability does not own it.

Exactly one of (`url`, `session_id`) must be supplied.

Scope: when `selector` is given, scraping is restricted to the matched
element subtree (text + tables inside it). Otherwise the whole document
body is used.

Extraction:
  - When `extract` (a natural-language description of what to pull out) is
    supplied and an LLM is wired into the ActivityContext, the handler asks
    the model to turn the page text into structured JSON. The LLM call is a
    narrow, read-only judgment on already-fetched content — it never selects
    actions (that would violate the planner spine).
  - When `extract` is omitted, or no LLM is available, or the LLM returns
    nothing parseable, the handler falls back to a deterministic heuristic:
    `{ "text": <page text>, "tables": [[...rows...], ...] }`.

Output: `{ url, data }` where `data` is either the LLM's parsed JSON object
or the heuristic `{text, tables}` shape. The raw page text is always
available under `data` (directly, or as `data["text"]` in the heuristic
path) so downstream nodes are never left empty-handed.

No credentials: cap.web_scrape reads public content or reuses an already-
authenticated session. It declares no secrets.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import CapabilityDefinition

logger = logging.getLogger(__name__)
CAP_REF = "cap.web_scrape"

_DEFAULT_TIMEOUT_MS = 15000
# Cap the page text we hand to the LLM so a huge page can't blow the
# prompt budget. The heuristic fallback returns the full text regardless.
_MAX_LLM_TEXT_CHARS = 20000


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str | None = Field(
        default=None,
        description=(
            "Absolute URL to open and scrape (http or https). Supply this OR "
            "`session_id`, not both. When set, a fresh browser session is "
            "opened and closed within this node."
        ),
    )
    session_id: str | None = Field(
        default=None,
        description=(
            "Handle of an existing browser session (from cap.open_url, "
            "cap.web_login, or browser.open_session) to scrape without "
            "navigating. Supply this OR `url`, not both. The session is left "
            "open for downstream nodes."
        ),
    )
    selector: str | None = Field(
        default=None,
        description=(
            "Optional CSS selector scoping the scrape to a single element "
            "subtree. When omitted the whole document body is scraped."
        ),
    )
    extract: str | None = Field(
        default=None,
        description=(
            "Optional natural-language description of what to pull out of the "
            "page (e.g. 'the order id and total amount'). When set and an LLM "
            "is available, the page text is turned into structured JSON. "
            "Otherwise a deterministic {text, tables} shape is returned."
        ),
    )
    wait_selector: str | None = Field(
        default=None,
        description=(
            "Optional CSS selector to wait for after navigation before "
            "scraping (only consulted when `url` is set). Use for JS-rendered "
            "pages whose content arrives after load."
        ),
    )
    timeout_ms: int = Field(
        default=_DEFAULT_TIMEOUT_MS,
        ge=1000,
        le=120000,
        description="Selector wait timeout (consulted only when a selector wait is needed).",
    )

    @model_validator(mode="after")
    def _check_source(self) -> _Inputs:
        if bool(self.url) == bool(self.session_id):
            raise ValueError(
                "web_scrape requires exactly one of `url` or `session_id`"
            )
        return self


class _Outputs(BaseModel):
    url: str = Field(description="The URL that was scraped (resolved from the live page).")
    data: dict[str, Any] = Field(
        description=(
            "Structured extraction. When `extract` + an LLM are available, the "
            "model's parsed JSON object; otherwise the heuristic "
            "{text, tables} shape."
        )
    )


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Scrape a web page and return its content. Either opens a fresh "
        "session for a given URL or reuses an existing browser session. "
        "Optional CSS `selector` scopes the scrape; optional `extract` "
        "natural-language hint turns the page text into structured JSON via "
        "the LLM, falling back to a deterministic {text, tables} shape."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("browser", "scrape", "extract"),
)


# ---------- JS payloads ----------------------------------------------------
#
# These are evaluated in the page context via session.evaluate(). The marker
# comments (AAKAAR_SCRAPE_*) double as the substring keys the FakeBrowserSession
# matches in tests, so they must stay stable.

_JS_URL = "/* AAKAAR_SCRAPE_URL */ document.location.href"

_JS_TEXT = """/* AAKAAR_SCRAPE_TEXT */
(function (sel) {
  var root = sel ? document.querySelector(sel) : document.body;
  if (!root) return "";
  return (root.innerText || root.textContent || "").trim();
})(%s)"""

_JS_TABLES = """/* AAKAAR_SCRAPE_TABLES */
(function (sel) {
  var scope = sel ? document.querySelector(sel) : document;
  if (!scope) return [];
  var tables = [];
  var els = scope.querySelectorAll("table");
  for (var t = 0; t < els.length; t++) {
    var rows = [];
    var trs = els[t].querySelectorAll("tr");
    for (var r = 0; r < trs.length; r++) {
      var cells = trs[r].querySelectorAll("th,td");
      var row = [];
      for (var c = 0; c < cells.length; c++) {
        row.push((cells[c].innerText || cells[c].textContent || "").trim());
      }
      if (row.length) rows.push(row);
    }
    if (rows.length) tables.push(rows);
  }
  return tables;
})(%s)"""


def _build_text_js(selector: str | None) -> str:
    return _JS_TEXT % (json.dumps(selector) if selector else "null")


def _build_tables_js(selector: str | None) -> str:
    return _JS_TABLES % (json.dumps(selector) if selector else "null")


_EXTRACT_SYSTEM = (
    "You extract structured data from web page text. Reply with ONE JSON "
    "object and nothing else: no prose, no markdown fences. Use null for any "
    "field you cannot find. Do not invent values."
)

_EXTRACT_USER = (
    "Extract the following from the page text below and return it as a JSON "
    "object.\n\nWhat to extract: {what}\n\nPage text:\n{text}"
)


def _coerce_text(value: object) -> str:
    """evaluate() returns a JSON-serializable object; normalize to text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _coerce_tables(value: object) -> list[list[list[str]]]:
    """Normalize the tables payload into list[list[list[str]]], dropping
    anything malformed rather than failing the node."""
    if not isinstance(value, list):
        return []
    tables: list[list[list[str]]] = []
    for table in value:
        if not isinstance(table, list):
            continue
        rows: list[list[str]] = []
        for row in table:
            if not isinstance(row, list):
                continue
            rows.append([_coerce_text(cell) for cell in row])
        if rows:
            tables.append(rows)
    return tables


def _parse_llm_json(raw: str) -> dict[str, Any] | None:
    """Parse the LLM's reply into a dict. Tolerates a leading/trailing code
    fence or stray prose by locating the outermost JSON object. Returns None
    when nothing parseable is found so the caller falls back to heuristics."""
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        # Strip a ```json ... ``` fence if the model added one despite the
        # instruction.
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(s[start : end + 1])
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def _llm_extract(
    ctx: ActivityContext, *, what: str, text: str
) -> dict[str, Any] | None:
    """Ask the wired LLM to turn page text into structured JSON. Returns None
    on any failure (no LLM, empty reply, unparseable) so the handler can fall
    back to the heuristic shape. Never raises out of the LLM path."""
    if ctx.llm is None or not text:
        return None
    user = _EXTRACT_USER.format(what=what, text=text[:_MAX_LLM_TEXT_CHARS])
    try:
        raw = await asyncio.to_thread(ctx.llm.complete_text, _EXTRACT_SYSTEM, user)
    except Exception:  # noqa: BLE001 — LLM is best-effort; degrade to heuristic.
        logger.warning("cap.web_scrape: LLM extraction failed; using heuristic", exc_info=True)
        return None
    return _parse_llm_json(_coerce_text(raw))


async def _scrape_session(
    session: Any, *, selector: str | None
) -> tuple[str, str, list[list[list[str]]]]:
    """Pull (url, text, tables) off a live session. Tables are best-effort:
    a page without any <table> simply yields []."""
    url = _coerce_text(await session.evaluate(_JS_URL))
    text = _coerce_text(await session.evaluate(_build_text_js(selector)))
    tables = _coerce_tables(await session.evaluate(_build_tables_js(selector)))
    return url, text, tables


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    url_in = inputs.get("url")
    session_id = inputs.get("session_id")
    selector = inputs.get("selector")
    extract = inputs.get("extract")
    wait_selector = inputs.get("wait_selector")
    timeout = int(inputs.get("timeout_ms", _DEFAULT_TIMEOUT_MS))

    if bool(url_in) == bool(session_id):
        # Defensive: the input schema enforces this, but the handler may be
        # driven directly in tests/tools without validation.
        raise RuntimeError(
            "cap.web_scrape requires exactly one of `url` or `session_id`"
        )

    logger.info(
        "cap.web_scrape start run_id=%s mode=%s selector=%r extract=%s",
        ctx.run_id,
        "url" if url_in else "session",
        selector,
        bool(extract),
    )

    if url_in:
        result = await _scrape_via_url(
            ctx,
            url=url_in,
            selector=selector,
            wait_selector=wait_selector,
            timeout=timeout,
        )
    else:
        result = await _scrape_via_session(
            ctx, session_id=str(session_id), selector=selector
        )

    page_url, text, tables = result

    data: dict[str, Any] | None = None
    if extract:
        data = await _llm_extract(ctx, what=extract, text=text)
    if data is None:
        data = {"text": text, "tables": tables}

    logger.info(
        "cap.web_scrape ok run_id=%s url=%s text_len=%d tables=%d structured=%s",
        ctx.run_id,
        page_url,
        len(text),
        len(tables),
        bool(extract) and "text" not in data,
    )
    return {"url": page_url or (url_in or ""), "data": data}


async def _scrape_via_url(
    ctx: ActivityContext,
    *,
    url: str,
    selector: str | None,
    wait_selector: str | None,
    timeout: int,
) -> tuple[str, str, list[list[list[str]]]]:
    if ctx.browser_pool is None:
        raise RuntimeError("cap.web_scrape with `url` requires a browser_pool")
    cm = ctx.browser_pool.checkout()
    session = await cm.__aenter__()
    try:
        await session.navigate(url)
        if wait_selector:
            await session.wait_for(wait_selector, timeout_ms=timeout)
        elif selector:
            await session.wait_for(selector, timeout_ms=timeout)
        page_url, text, tables = await _scrape_session(session, selector=selector)
    finally:
        # We own this session; always tear it down.
        await cm.__aexit__(None, None, None)
    return page_url or url, text, tables


async def _scrape_via_session(
    ctx: ActivityContext, *, session_id: str, selector: str | None
) -> tuple[str, str, list[list[list[str]]]]:
    # Import lazily to avoid coupling module import to the browser package.
    from aakaar.interpreter.activities.browser import _stash_key

    holder = ctx.session_state.get(_stash_key(session_id))
    if holder is None:
        raise RuntimeError(
            f"cap.web_scrape: no live browser session for id {session_id!r}; "
            "open one upstream (cap.open_url / cap.web_login) first"
        )
    session = holder.session
    # Do NOT close — the session is owned by whoever opened it.
    return await _scrape_session(session, selector=selector)
