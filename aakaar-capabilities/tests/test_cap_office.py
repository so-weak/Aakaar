"""Round-trip tests for the office caps: excel_read/excel_write, word_read/word_write.

Each pair is guarded by pytest.importorskip for its heavy lib (openpyxl,
python-docx). The ctx is faked with an in-memory object store (read_object /
write_object backed by a dict), matching the pattern used by the browser cap
tests.
"""

from __future__ import annotations

from typing import Any

import pytest

from aakaar_caps.context import CapabilityContext


def _ctx() -> tuple[CapabilityContext, dict[str, bytes]]:
    store: dict[str, bytes] = {}

    async def writer(key: str, data: bytes) -> str:
        uri = "aakaar://t/x/" + key
        store[uri] = data
        return uri

    async def reader(uri: str) -> bytes:
        return store[uri]

    ctx = CapabilityContext(run_id="office", object_writer=writer, object_reader=reader)
    return ctx, store


# --------------------------------------------------------------------------- excel


async def test_excel_write_then_read_list_of_lists() -> None:
    pytest.importorskip("openpyxl")
    from aakaar_caps.caps import excel_read, excel_write

    ctx, _store = _ctx()
    rows = [["a", "b"], [1, 2], [3, 4]]
    w = await excel_write.run(ctx, {"rows": rows, "sheet_name": "Data"})
    assert w["report_uri"].startswith("aakaar://")
    assert w["row_count"] == 3

    r = await excel_read.run(ctx, {"file_uri": w["report_uri"]})
    assert r["sheet_names"] == ["Data"]
    assert r["sheets"]["Data"] == [["a", "b"], [1, 2], [3, 4]]


async def test_excel_write_dicts_makes_header() -> None:
    pytest.importorskip("openpyxl")
    from aakaar_caps.caps import excel_read, excel_write

    ctx, _store = _ctx()
    rows = [{"name": "x", "qty": 1}, {"name": "y", "qty": 2}]
    w = await excel_write.run(ctx, {"rows": rows})
    assert w["row_count"] == 2

    r = await excel_read.run(ctx, {"file_uri": w["report_uri"]})
    grid = next(iter(r["sheets"].values()))
    assert grid[0] == ["name", "qty"]
    assert grid[1] == ["x", 1]


async def test_excel_read_sheet_not_found() -> None:
    pytest.importorskip("openpyxl")
    from aakaar_caps.caps import excel_read, excel_write

    ctx, _store = _ctx()
    w = await excel_write.run(ctx, {"rows": [["a"]], "sheet_name": "Only"})
    with pytest.raises(ValueError):
        await excel_read.run(ctx, {"file_uri": w["report_uri"], "sheet": "Missing"})


async def test_excel_read_max_rows() -> None:
    pytest.importorskip("openpyxl")
    from aakaar_caps.caps import excel_read, excel_write

    ctx, _store = _ctx()
    w = await excel_write.run(ctx, {"rows": [[i] for i in range(10)]})
    r = await excel_read.run(ctx, {"file_uri": w["report_uri"], "max_rows": 3})
    assert len(next(iter(r["sheets"].values()))) == 3


# --------------------------------------------------------------------------- word


async def test_word_write_then_read() -> None:
    pytest.importorskip("docx")
    from aakaar_caps.caps import word_read, word_write

    ctx, _store = _ctx()
    paras: list[Any] = [
        {"heading": "Section 1", "level": 1},
        "First body paragraph.",
        "Second body paragraph.",
    ]
    w = await word_write.run(ctx, {"paragraphs": paras, "title": "Report"})
    assert w["document_uri"].startswith("aakaar://")
    assert w["paragraph_count"] == 3

    r = await word_read.run(ctx, {"file_uri": w["document_uri"]})
    assert "Report" in r["text"]
    assert "Section 1" in r["paragraphs"]
    assert "First body paragraph." in r["paragraphs"]
    assert "Second body paragraph." in r["paragraphs"]
    assert isinstance(r["tables"], list)
