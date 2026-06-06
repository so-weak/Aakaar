"""Tests for cap.web_scrape.

Covers:
  - URL mode: opens a fresh session, navigates, scrapes text + tables via
    evaluate(), tears the session down.
  - session_id reuse: scrapes an upstream session without closing it.
  - LLM extraction: with ctx.llm primed, page text is turned into a
    structured JSON dict (the `data` shape switches from {text,tables}).
  - LLM fallback: empty / no LLM degrades to the heuristic {text, tables}.
  - Input validation: exactly one of (url, session_id) is required.
  - Pure helpers: JSON parsing, table coercion.

Driven directly against the handler with a hand-built ActivityContext and
fakes (FakeBrowserPool / FakeBrowserSession / FakeLLMClient), no executor.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from aakaar.capabilities.web.web_scrape import (
    CAP_REF,
    _coerce_tables,
    _Inputs,
    _parse_llm_json,
    definition,
    handler,
)
from aakaar.interpreter.activities.browser import _SessionHolder, _stash_key
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.planner.llm import FakeLLMClient
from aakaar.shared.registry import build_default_registry
from aakaar.storage import LocalFsObjectStore
from aakaar.vault import LocalVault
from aakaar.workers.browser import FakeBrowserPool, FakeBrowserSession

_PAGE_URL = "https://shop.aakaar.test/orders/42"
_PAGE_TEXT = "Order 42\nTotal: 199.00 USD\nStatus: Shipped"
_TABLE = [[["Item", "Qty"], ["Widget", "3"]]]


def _make_ctx(tmp_path: Path, *, pool=None, llm=None) -> ActivityContext:
    tenant_id = uuid.uuid4()
    return ActivityContext(
        tenant_id=tenant_id,
        run_id=uuid.uuid4(),
        registry=build_default_registry(),
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=LocalVault(tmp_path / "vault"),
        browser_pool=pool,
        llm=llm,
        granted_capabilities={CAP_REF: {"primary": {"vault_ref": "", "input_defaults": {}}}},
    )


def _scripted_session() -> FakeBrowserSession:
    # Keyed by the AAKAAR_SCRAPE_* markers embedded in the JS payloads.
    return FakeBrowserSession(
        evaluate_responses={
            "AAKAAR_SCRAPE_URL": _PAGE_URL,
            "AAKAAR_SCRAPE_TEXT": _PAGE_TEXT,
            "AAKAAR_SCRAPE_TABLES": _TABLE,
        }
    )


# ---------- definition sanity ---------------------------------------------


def test_definition_shape() -> None:
    assert definition.ref == "cap.web_scrape"
    assert definition.secrets == ()
    assert "scrape" in definition.tags


# ---------- input validation ----------------------------------------------


def test_requires_exactly_one_source() -> None:
    with pytest.raises(ValidationError):
        _Inputs()  # neither url nor session_id
    with pytest.raises(ValidationError):
        _Inputs(url="https://x.test", session_id="abc")  # both
    # Each alone validates.
    assert _Inputs(url="https://x.test").url == "https://x.test"
    assert _Inputs(session_id="abc").session_id == "abc"


def test_inputs_forbid_extra() -> None:
    with pytest.raises(ValidationError):
        _Inputs(url="https://x.test", bogus=1)


# ---------- pure helpers ---------------------------------------------------


def test_parse_llm_json_variants() -> None:
    assert _parse_llm_json('{"a": 1}') == {"a": 1}
    # Fenced output despite instructions.
    assert _parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}
    # Prose around the object.
    assert _parse_llm_json('Here you go: {"a": 1} done') == {"a": 1}
    # Not an object / unparseable -> None (caller falls back to heuristic).
    assert _parse_llm_json("") is None
    assert _parse_llm_json("[1, 2]") is None
    assert _parse_llm_json("not json") is None


def test_coerce_tables_drops_malformed() -> None:
    assert _coerce_tables([[["a", "b"]], "junk", [["c"]]]) == [[["a", "b"]], [["c"]]]
    assert _coerce_tables("nope") == []
    # Cells coerced to strings; non-list rows dropped.
    assert _coerce_tables([[[1, 2], "x"]]) == [[["1", "2"]]]


# ---------- URL mode -------------------------------------------------------


@pytest.mark.asyncio
async def test_url_mode_heuristic(tmp_path: Path) -> None:
    sess = _scripted_session()
    pool = FakeBrowserPool(next_sessions=[sess])
    ctx = _make_ctx(tmp_path, pool=pool)

    out = await handler(ctx, {"url": _PAGE_URL})

    assert out["url"] == _PAGE_URL
    assert out["data"] == {"text": _PAGE_TEXT, "tables": _TABLE}
    # Fresh session was navigated and then closed (we own it in URL mode).
    kinds = [c[0] for c in sess.calls]
    assert kinds[0] == "navigate"
    assert sess.closed
    # No selector wait was requested.
    assert not any(c[0] == "wait_for" for c in sess.calls)


@pytest.mark.asyncio
async def test_url_mode_waits_for_selector(tmp_path: Path) -> None:
    sess = _scripted_session()
    pool = FakeBrowserPool(next_sessions=[sess])
    ctx = _make_ctx(tmp_path, pool=pool)

    await handler(
        ctx,
        {"url": _PAGE_URL, "wait_selector": "main[data-ready]", "timeout_ms": 5000},
    )
    waits = [c for c in sess.calls if c[0] == "wait_for"]
    assert len(waits) == 1
    assert waits[0][1]["selector"] == "main[data-ready]"
    assert waits[0][1]["timeout_ms"] == 5000


@pytest.mark.asyncio
async def test_url_mode_selector_scopes_js(tmp_path: Path) -> None:
    sess = _scripted_session()
    pool = FakeBrowserPool(next_sessions=[sess])
    ctx = _make_ctx(tmp_path, pool=pool)

    await handler(ctx, {"url": _PAGE_URL, "selector": "#summary"})
    # The selector is JSON-embedded into the text/tables JS payloads, and the
    # selector itself is also waited for (absent an explicit wait_selector).
    eval_js = [c[1]["js"] for c in sess.calls if c[0] == "evaluate"]
    text_js = next(j for j in eval_js if "AAKAAR_SCRAPE_TEXT" in j)
    assert '"#summary"' in text_js
    waits = [c[1]["selector"] for c in sess.calls if c[0] == "wait_for"]
    assert waits == ["#summary"]


@pytest.mark.asyncio
async def test_url_mode_requires_browser_pool(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, pool=None)
    with pytest.raises(RuntimeError, match="browser_pool"):
        await handler(ctx, {"url": _PAGE_URL})


# ---------- session reuse mode ---------------------------------------------


@pytest.mark.asyncio
async def test_session_mode_reuses_and_does_not_close(tmp_path: Path) -> None:
    sess = _scripted_session()
    ctx = _make_ctx(tmp_path, pool=None)
    # Pretend an upstream node opened this session.
    ctx.session_state[_stash_key(sess.id)] = _SessionHolder(cm=None, session=sess)

    out = await handler(ctx, {"session_id": sess.id})

    assert out["url"] == _PAGE_URL
    assert out["data"] == {"text": _PAGE_TEXT, "tables": _TABLE}
    # No navigate (reuse), and crucially not closed — downstream owns it.
    assert not any(c[0] == "navigate" for c in sess.calls)
    assert not sess.closed
    assert any(c[0] == "evaluate" for c in sess.calls)


@pytest.mark.asyncio
async def test_session_mode_missing_session_raises(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, pool=None)
    with pytest.raises(RuntimeError, match="no live browser session"):
        await handler(ctx, {"session_id": "ghost"})


# ---------- LLM extraction -------------------------------------------------


@pytest.mark.asyncio
async def test_extract_with_llm_returns_structured_json(tmp_path: Path) -> None:
    sess = _scripted_session()
    pool = FakeBrowserPool(next_sessions=[sess])
    llm = FakeLLMClient(text_replies=['{"order_id": "42", "total": "199.00 USD"}'])
    ctx = _make_ctx(tmp_path, pool=pool, llm=llm)

    out = await handler(
        ctx, {"url": _PAGE_URL, "extract": "the order id and total"}
    )
    assert out["data"] == {"order_id": "42", "total": "199.00 USD"}
    # The LLM saw the page text in its user prompt.
    assert llm.text_calls, "expected complete_text to be called"
    _system, user = llm.text_calls[0]
    assert "the order id and total" in user
    assert "Order 42" in user


@pytest.mark.asyncio
async def test_extract_with_empty_llm_reply_falls_back(tmp_path: Path) -> None:
    sess = _scripted_session()
    pool = FakeBrowserPool(next_sessions=[sess])
    llm = FakeLLMClient(text_replies=[])  # exhausted -> "" -> unparseable
    ctx = _make_ctx(tmp_path, pool=pool, llm=llm)

    out = await handler(ctx, {"url": _PAGE_URL, "extract": "anything"})
    # Falls back to the deterministic heuristic.
    assert out["data"] == {"text": _PAGE_TEXT, "tables": _TABLE}


@pytest.mark.asyncio
async def test_extract_with_no_llm_falls_back(tmp_path: Path) -> None:
    sess = _scripted_session()
    pool = FakeBrowserPool(next_sessions=[sess])
    ctx = _make_ctx(tmp_path, pool=pool, llm=None)

    out = await handler(ctx, {"url": _PAGE_URL, "extract": "anything"})
    assert out["data"] == {"text": _PAGE_TEXT, "tables": _TABLE}


# ---------- handler-level guard --------------------------------------------


@pytest.mark.asyncio
async def test_handler_rejects_both_sources(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, pool=FakeBrowserPool())
    with pytest.raises(RuntimeError, match="exactly one"):
        await handler(ctx, {"url": _PAGE_URL, "session_id": "abc"})
