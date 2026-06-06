"""Tests for cap.doc_extract.

Drives the handler with a hand-built ActivityContext, a LocalFsObjectStore
(documents written into tenant storage as aakaar:// URIs) and a
FakeLLMClient, covering:
  - csv / json / txt happy paths read from the object store
  - format override and extension-based detection (incl. unknown -> txt)
  - the optional LLM extraction pass (used / skipped / unparseable)
  - definition shape + input-schema validation
  - the pure helpers (_detect_format, _key_from_uri, _pdf_tables_from_text,
    _strip_code_fence, _loads_lenient)
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from aakaar.capabilities.data.doc_extract import (
    CAP_REF,
    _detect_format,
    _key_from_uri,
    _loads_lenient,
    _pdf_tables_from_text,
    _strip_code_fence,
    definition,
    handler,
)
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.planner.llm import FakeLLMClient


def _ctx(tmp_path: Path, llm: Any = None) -> ActivityContext:
    from aakaar.shared.registry import build_default_registry
    from aakaar.storage import LocalFsObjectStore
    from aakaar.vault import LocalVault

    return ActivityContext(
        tenant_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        registry=build_default_registry(),
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=LocalVault(tmp_path / "vault"),
        llm=llm,
    )


def _put(ctx: ActivityContext, key: str, data: bytes) -> str:
    return ctx.object_store.put(str(ctx.tenant_id), key, data).uri


# --------------------------------------------------------------------------
# Happy paths from object store
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_csv_returns_rows(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    uri = _put(ctx, "report.csv", b"name,amount\nAlice,100\nBob,200\n")
    out = await handler(ctx, {"uri": uri})
    assert out["type"] == "csv"
    assert out["text"] is None
    assert out["data"] == [
        {"name": "Alice", "amount": 100},
        {"name": "Bob", "amount": 200},
    ]
    assert out["extracted"] is None


@pytest.mark.asyncio
async def test_csv_empty_is_empty_list(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    uri = _put(ctx, "empty.csv", b"   \n")
    out = await handler(ctx, {"uri": uri})
    assert out == {
        "type": "csv",
        "data": [],
        "text": None,
        "tables": None,
        "extracted": None,
    }


@pytest.mark.asyncio
async def test_json_returns_parsed(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    payload = {"invoice": "INV-42", "lines": [{"sku": "A", "qty": 2}]}
    uri = _put(ctx, "doc.json", json.dumps(payload).encode())
    out = await handler(ctx, {"uri": uri})
    assert out["type"] == "json"
    assert out["data"] == payload
    assert out["text"] is None


@pytest.mark.asyncio
async def test_json_invalid_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    uri = _put(ctx, "bad.json", b"{not json")
    with pytest.raises(RuntimeError, match="not valid JSON"):
        await handler(ctx, {"uri": uri})


@pytest.mark.asyncio
async def test_txt_returns_text(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    uri = _put(ctx, "notes.txt", b"line one\nline two\n")
    out = await handler(ctx, {"uri": uri})
    assert out["type"] == "txt"
    assert out["text"] == "line one\nline two\n"
    assert out["data"] is None


@pytest.mark.asyncio
async def test_unknown_extension_falls_back_to_txt(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    uri = _put(ctx, "blob.dat", b"raw bytes content")
    out = await handler(ctx, {"uri": uri})
    assert out["type"] == "txt"
    assert out["text"] == "raw bytes content"


@pytest.mark.asyncio
async def test_format_override_wins_over_extension(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    # File named .txt but actually JSON; override forces the json parser.
    uri = _put(ctx, "mislabeled.txt", b'{"a": 1}')
    out = await handler(ctx, {"uri": uri, "format": "json"})
    assert out["type"] == "json"
    assert out["data"] == {"a": 1}


@pytest.mark.asyncio
async def test_decoding_is_error_tolerant(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    # Invalid utf-8 byte; should be replaced, not raise.
    uri = _put(ctx, "weird.txt", b"hello \xff world")
    out = await handler(ctx, {"uri": uri})
    assert out["type"] == "txt"
    assert "hello" in out["text"] and "world" in out["text"]


# --------------------------------------------------------------------------
# xlsx (pandas/openpyxl are installed in v1) — write a real workbook
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_xlsx_returns_rows(tmp_path: Path) -> None:
    pd = pytest.importorskip("pandas")
    pytest.importorskip("openpyxl")
    import io

    df = pd.DataFrame([{"item": "x", "qty": 1}, {"item": "y", "qty": 3}])
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    ctx = _ctx(tmp_path)
    uri = _put(ctx, "sheet.xlsx", buf.getvalue())
    out = await handler(ctx, {"uri": uri})
    assert out["type"] == "xlsx"
    assert out["data"] == [{"item": "x", "qty": 1}, {"item": "y", "qty": 3}]


# --------------------------------------------------------------------------
# Optional LLM extraction pass
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_with_llm_returns_structured(tmp_path: Path) -> None:
    llm = FakeLLMClient(text_replies=[json.dumps({"total": 300})])
    ctx = _ctx(tmp_path, llm)
    uri = _put(ctx, "report.csv", b"name,amount\nAlice,100\nBob,200\n")
    out = await handler(ctx, {"uri": uri, "extract": "sum the amounts"})
    assert out["type"] == "csv"
    assert out["extracted"] == {"total": 300}
    # The instruction and the rendered content reached the model.
    system, user = llm.text_calls[0]
    assert "sum the amounts" in user
    assert "Alice" in user


@pytest.mark.asyncio
async def test_extract_tolerates_code_fenced_json(tmp_path: Path) -> None:
    reply = "```json\n" + json.dumps({"k": "v"}) + "\n```"
    llm = FakeLLMClient(text_replies=[reply])
    ctx = _ctx(tmp_path, llm)
    uri = _put(ctx, "notes.txt", b"some content")
    out = await handler(ctx, {"uri": uri, "extract": "pull k"})
    assert out["extracted"] == {"k": "v"}


@pytest.mark.asyncio
async def test_extract_without_llm_skips_pass(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, llm=None)
    uri = _put(ctx, "notes.txt", b"some content")
    out = await handler(ctx, {"uri": uri, "extract": "pull something"})
    assert out["type"] == "txt"
    assert out["extracted"] is None


@pytest.mark.asyncio
async def test_extract_empty_llm_output_is_none(tmp_path: Path) -> None:
    # FakeLLMClient with no queued replies returns "" -> extracted stays None.
    ctx = _ctx(tmp_path, FakeLLMClient())
    uri = _put(ctx, "notes.txt", b"content")
    out = await handler(ctx, {"uri": uri, "extract": "x"})
    assert out["extracted"] is None


@pytest.mark.asyncio
async def test_extract_unparseable_llm_output_is_none(tmp_path: Path) -> None:
    llm = FakeLLMClient(text_replies=["not json at all"])
    ctx = _ctx(tmp_path, llm)
    uri = _put(ctx, "notes.txt", b"content")
    out = await handler(ctx, {"uri": uri, "extract": "x"})
    assert out["extracted"] is None


@pytest.mark.asyncio
async def test_extract_llm_exception_is_none(tmp_path: Path) -> None:
    class _BoomLLM:
        def complete_text(self, system: str, user: str) -> str:
            raise RuntimeError("rate limited")

    ctx = _ctx(tmp_path, _BoomLLM())
    uri = _put(ctx, "notes.txt", b"content")
    out = await handler(ctx, {"uri": uri, "extract": "x"})
    assert out["extracted"] is None


# --------------------------------------------------------------------------
# Definition + input schema
# --------------------------------------------------------------------------


def test_definition_shape() -> None:
    assert definition.ref == CAP_REF == "cap.doc_extract"
    assert definition.secrets == ()
    assert "data" in definition.tags


def test_input_schema_forbids_extra_keys() -> None:
    with pytest.raises(ValidationError):
        definition.input_schema(uri="aakaar://t/x/y.csv", bogus=1)


def test_input_schema_requires_uri() -> None:
    with pytest.raises(ValidationError):
        definition.input_schema(format="csv")


def test_invalid_format_override_raises() -> None:
    with pytest.raises(ValueError, match="unsupported format override"):
        _detect_format("aakaar://t/x/y.dat", "xml")


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def test_detect_format_by_extension() -> None:
    assert _detect_format("aakaar://t/x/a.csv", None) == "csv"
    assert _detect_format("aakaar://t/x/a.XLSX", None) == "xlsx"
    assert _detect_format("aakaar://t/x/a.json", None) == "json"
    assert _detect_format("aakaar://t/x/a.pdf", None) == "pdf"
    assert _detect_format("aakaar://t/x/a.log", None) == "txt"
    assert _detect_format("aakaar://t/x/noext", None) == "txt"


def test_detect_format_override_normalizes_case() -> None:
    assert _detect_format("aakaar://t/x/a.csv", "JSON") == "json"


def test_key_from_uri() -> None:
    assert _key_from_uri("aakaar://t/tenant-1/sub/dir/report.csv") == "report.csv"
    assert _key_from_uri("plainfile.txt") == "plainfile.txt"


def test_pdf_tables_groups_multicolumn_lines() -> None:
    page = (
        "Title only\n"
        "Item    Qty    Price\n"
        "Widget   2     9.99\n"
        "Gadget   1     4.50\n"
        "\n"
        "footer line\n"
    )
    tables = _pdf_tables_from_text(page)
    assert tables == [
        [
            ["Item", "Qty", "Price"],
            ["Widget", "2", "9.99"],
            ["Gadget", "1", "4.50"],
        ]
    ]


def test_pdf_tables_ignores_single_column_runs() -> None:
    assert _pdf_tables_from_text("just\nplain\nlines\n") == []


def test_strip_code_fence_variants() -> None:
    assert _strip_code_fence("```json\n{}\n```") == "{}"
    assert _strip_code_fence("```\n[]\n```") == "[]"
    assert _strip_code_fence('{"a":1}') == '{"a":1}'


def test_loads_lenient_extracts_embedded_object() -> None:
    assert _loads_lenient('noise {"a": 1} trailing') == {"a": 1}
    with pytest.raises(ValueError):
        _loads_lenient("definitely not json")
