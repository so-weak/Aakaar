"""cap.web_scrape — scrape a page; optionally LLM-extract structured JSON.

Opens a fresh session at `url` or reuses an existing `session_id`, pulls
{text, tables}, and (when `extract` + an LLM are available) turns the text into
structured JSON via the portable complete_text seam. Shared: identical on the
server and a remote agent (the LLM call is proxied to the server on the agent).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aakaar_caps.browser.state import get_session
from aakaar_caps.context import CapabilityContext, CapabilityError
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.web_scrape"

_DEFAULT_TIMEOUT_MS = 15000
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
        default=_DEFAULT_TIMEOUT_MS, ge=1000, le=120000,
        description="Selector wait timeout (consulted only when a selector wait is needed).",
    )

    @model_validator(mode="after")
    def _check_source(self) -> _Inputs:
        if bool(self.url) == bool(self.session_id):
            raise ValueError("web_scrape requires exactly one of `url` or `session_id`")
        return self


class _Outputs(BaseModel):
    url: str = Field(description="The URL that was scraped (resolved from the live page).")
    data: dict[str, Any] = Field(
        description=(
            "Structured extraction. When `extract` + an LLM are available, the "
            "model's parsed JSON object; otherwise the heuristic {text, tables} shape."
        )
    )


SPEC = CapabilitySpec(
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

_EXTRACT_SYSTEM = (
    "You extract structured data from web page text. Reply with ONE JSON "
    "object and nothing else: no prose, no markdown fences. Use null for any "
    "field you cannot find. Do not invent values."
)
_EXTRACT_USER = (
    "Extract the following from the page text below and return it as a JSON "
    "object.\n\nWhat to extract: {what}\n\nPage text:\n{text}"
)


def _build_text_js(selector: str | None) -> str:
    return _JS_TEXT % (json.dumps(selector) if selector else "null")


def _build_tables_js(selector: str | None) -> str:
    return _JS_TABLES % (json.dumps(selector) if selector else "null")


def _coerce_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _coerce_tables(value: object) -> list[list[list[str]]]:
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
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(s[start : end + 1])
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def _llm_extract(ctx: CapabilityContext, *, what: str, text: str) -> dict[str, Any] | None:
    if ctx.text_completer is None or not text:
        return None
    user = _EXTRACT_USER.format(what=what, text=text[:_MAX_LLM_TEXT_CHARS])
    try:
        raw = await asyncio.to_thread(ctx.complete_text, _EXTRACT_SYSTEM, user)
    except Exception:  # noqa: BLE001 — best-effort; degrade to heuristic.
        logger.warning("cap.web_scrape: LLM extraction failed; using heuristic", exc_info=True)
        return None
    return _parse_llm_json(_coerce_text(raw))


async def _scrape_session(session: Any, *, selector: str | None) -> tuple[str, str, list[list[list[str]]]]:
    url = _coerce_text(await session.evaluate(_JS_URL))
    text = _coerce_text(await session.evaluate(_build_text_js(selector)))
    tables = _coerce_tables(await session.evaluate(_build_tables_js(selector)))
    return url, text, tables


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    url_in = inputs.get("url")
    session_id = inputs.get("session_id")
    selector = inputs.get("selector")
    extract = inputs.get("extract")
    wait_selector = inputs.get("wait_selector")
    timeout = int(inputs.get("timeout_ms", _DEFAULT_TIMEOUT_MS))

    if bool(url_in) == bool(session_id):
        raise RuntimeError("cap.web_scrape requires exactly one of `url` or `session_id`")

    logger.info("cap.web_scrape start run_id=%s mode=%s extract=%s", ctx.run_id, "url" if url_in else "session", bool(extract))

    if url_in:
        if ctx.browser_pool is None:
            raise CapabilityError("cap.web_scrape with `url` requires a browser_pool")
        cm = ctx.browser_pool.checkout()
        session = await cm.__aenter__()
        try:
            await session.navigate(url_in)
            if wait_selector:
                await session.wait_for(wait_selector, timeout_ms=timeout)
            elif selector:
                await session.wait_for(selector, timeout_ms=timeout)
            page_url, text, tables = await _scrape_session(session, selector=selector)
            page_url = page_url or url_in
        finally:
            await cm.__aexit__(None, None, None)
    else:
        session = get_session(ctx.session_state, str(session_id))
        page_url, text, tables = await _scrape_session(session, selector=selector)

    data: dict[str, Any] | None = None
    if extract:
        data = await _llm_extract(ctx, what=extract, text=text)
    if data is None:
        data = {"text": text, "tables": tables}

    logger.info("cap.web_scrape ok run_id=%s url=%s text_len=%d tables=%d", ctx.run_id, page_url, len(text), len(tables))
    return {"url": page_url or (url_in or ""), "data": data}
