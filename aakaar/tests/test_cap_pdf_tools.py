"""Tests for cap.pdf_tools.

Drives the handler with a hand-built ActivityContext and a
LocalFsObjectStore. Small PDFs are generated with pypdf's
``add_blank_page`` (no reportlab dependency) and put into the object store,
then read back to verify page counts after each op. Covers:
  - count_pages, merge, extract_pages, split happy paths (round-tripped
    through the object store)
  - source-arity and input validation
  - definition shape + input-schema validation
  - the pure page-selector helpers (_parse_pages, _expand_entry)
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from aakaar.capabilities.data.pdf_tools import (
    CAP_REF,
    _expand_entry,
    _output_key,
    _parse_pages,
    definition,
    handler,
)
from aakaar.interpreter.activities.types import ActivityContext

pytest.importorskip("pypdf")


def _ctx(tmp_path: Path) -> ActivityContext:
    from aakaar.shared.registry import build_default_registry
    from aakaar.storage import LocalFsObjectStore
    from aakaar.vault import LocalVault

    return ActivityContext(
        tenant_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        registry=build_default_registry(),
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=LocalVault(tmp_path / "vault"),
    )


def _make_pdf(n_pages: int) -> bytes:
    import pypdf

    writer = pypdf.PdfWriter()
    for _ in range(n_pages):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _put_pdf(ctx: ActivityContext, key: str, n_pages: int) -> str:
    return ctx.object_store.put(str(ctx.tenant_id), key, _make_pdf(n_pages)).uri


def _page_count(ctx: ActivityContext, uri: str) -> int:
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(ctx.object_store.get(uri)))
    return len(reader.pages)


# --------------------------------------------------------------------------
# Happy paths through the object store
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_pages(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    uri = _put_pdf(ctx, "doc.pdf", 3)
    out = await handler(ctx, {"op": "count_pages", "sources": [uri]})
    assert out == {"result_uris": [], "count": 3}


@pytest.mark.asyncio
async def test_merge(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    a = _put_pdf(ctx, "a.pdf", 2)
    b = _put_pdf(ctx, "b.pdf", 3)
    out = await handler(ctx, {"op": "merge", "sources": [a, b]})
    assert out["count"] == 1
    assert len(out["result_uris"]) == 1
    merged = out["result_uris"][0]
    assert merged.startswith("aakaar://t/")
    assert _page_count(ctx, merged) == 5


@pytest.mark.asyncio
async def test_extract_pages(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    uri = _put_pdf(ctx, "doc.pdf", 6)
    out = await handler(
        ctx, {"op": "extract_pages", "sources": [uri], "pages": [1, "3-5"]}
    )
    assert out["count"] == 1
    extracted = out["result_uris"][0]
    # pages 1, 3, 4, 5 -> 4 pages
    assert _page_count(ctx, extracted) == 4


@pytest.mark.asyncio
async def test_split_all_pages(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    uri = _put_pdf(ctx, "doc.pdf", 4)
    out = await handler(ctx, {"op": "split", "sources": [uri]})
    assert out["count"] == 4
    assert len(out["result_uris"]) == 4
    for u in out["result_uris"]:
        assert _page_count(ctx, u) == 1


@pytest.mark.asyncio
async def test_split_selected_pages(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    uri = _put_pdf(ctx, "doc.pdf", 5)
    out = await handler(ctx, {"op": "split", "sources": [uri], "pages": ["2-3"]})
    assert out["count"] == 2
    for u in out["result_uris"]:
        assert _page_count(ctx, u) == 1


@pytest.mark.asyncio
async def test_output_prefix_respected(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    uri = _put_pdf(ctx, "doc.pdf", 1)
    out = await handler(
        ctx, {"op": "merge", "sources": [uri], "output_prefix": "custom/out"}
    )
    assert "/custom/out/" in out["result_uris"][0]


# --------------------------------------------------------------------------
# Validation / arity
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_sources_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(RuntimeError, match="must not be empty"):
        await handler(ctx, {"op": "count_pages", "sources": []})


@pytest.mark.asyncio
async def test_count_pages_rejects_multiple_sources(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    a = _put_pdf(ctx, "a.pdf", 1)
    b = _put_pdf(ctx, "b.pdf", 1)
    with pytest.raises(RuntimeError, match="exactly one source"):
        await handler(ctx, {"op": "count_pages", "sources": [a, b]})


@pytest.mark.asyncio
async def test_extract_pages_requires_pages(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    uri = _put_pdf(ctx, "doc.pdf", 2)
    with pytest.raises(RuntimeError, match="requires `pages`"):
        await handler(ctx, {"op": "extract_pages", "sources": [uri]})


@pytest.mark.asyncio
async def test_extract_pages_out_of_range_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    uri = _put_pdf(ctx, "doc.pdf", 2)
    with pytest.raises(RuntimeError, match="out of range"):
        await handler(ctx, {"op": "extract_pages", "sources": [uri], "pages": [5]})


# --------------------------------------------------------------------------
# Definition + input schema
# --------------------------------------------------------------------------


def test_definition_shape() -> None:
    assert definition.ref == CAP_REF == "cap.pdf_tools"
    assert definition.secrets == ()
    assert "pdf" in definition.tags


def test_input_schema_forbids_extra_keys() -> None:
    with pytest.raises(ValidationError):
        definition.input_schema(op="merge", sources=["aakaar://t/x/a.pdf"], bogus=1)


def test_input_schema_rejects_unknown_op() -> None:
    with pytest.raises(ValidationError):
        definition.input_schema(op="rotate", sources=["aakaar://t/x/a.pdf"])


def test_input_schema_requires_sources() -> None:
    with pytest.raises(ValidationError):
        definition.input_schema(op="count_pages")


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def test_parse_pages_mixed_and_order_preserved() -> None:
    # 1-based [1, "3-5", 8] against a 10-page doc -> 0-based [0,2,3,4,7].
    assert _parse_pages([1, "3-5", 8], 10) == [0, 2, 3, 4, 7]


def test_parse_pages_allows_duplicates() -> None:
    assert _parse_pages([2, 2], 5) == [1, 1]


def test_parse_pages_out_of_range() -> None:
    with pytest.raises(RuntimeError, match="out of range"):
        _parse_pages([6], 3)


def test_parse_pages_empty_document() -> None:
    with pytest.raises(RuntimeError, match="no pages"):
        _parse_pages([1], 0)


def test_expand_entry_int_and_range() -> None:
    assert _expand_entry(4) == [4]
    assert _expand_entry("2-4") == [2, 3, 4]
    assert _expand_entry(" 7 ") == [7]


def test_expand_entry_reversed_range() -> None:
    with pytest.raises(RuntimeError, match="reversed"):
        _expand_entry("5-2")


def test_expand_entry_malformed() -> None:
    with pytest.raises(RuntimeError, match="malformed"):
        _expand_entry("abc")


def test_expand_entry_rejects_bool() -> None:
    with pytest.raises(RuntimeError, match="invalid page entry"):
        _expand_entry(True)


def test_output_key_with_and_without_prefix() -> None:
    assert _output_key("runs/x/pdf_tools").startswith("runs/x/pdf_tools/")
    assert _output_key("runs/x/pdf_tools").endswith(".pdf")
    assert _output_key("", suffix="-p01").endswith("-p01.pdf")
    assert "/" not in _output_key("")
