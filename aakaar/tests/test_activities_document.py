"""document.* activity tests — real happy paths against tmp-backed storage."""

from __future__ import annotations

import io
import json
import uuid
from pathlib import Path

import pytest

from aakaar.interpreter.activities import build_default_activities
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import build_default_registry
from aakaar.storage import LocalFsObjectStore
from aakaar.storage.object_store import make_uri
from aakaar.vault import LocalVault


def _actx(tmp_path: Path) -> ActivityContext:
    return ActivityContext(
        tenant_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        registry=build_default_registry(),
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=LocalVault(tmp_path / "vault"),
    )


def _put_xlsx(actx: ActivityContext, key: str, rows: list[dict], sheet_name: str = "Sheet1") -> str:
    import pandas as pd

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, sheet_name=sheet_name, index=False)
    obj = actx.object_store.put(str(actx.tenant_id), key, buf.getvalue())
    return obj.uri


def _put_csv(actx: ActivityContext, key: str, text: str) -> str:
    obj = actx.object_store.put(str(actx.tenant_id), key, text.encode("utf-8"))
    return obj.uri


def _make_pdf(text_per_page: list[str]) -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for page_text in text_per_page:
        c.drawString(72, 720, page_text)
        c.showPage()
    c.save()
    return buf.getvalue()


# ---------- registration --------------------------------------------------


def test_document_activities_registered() -> None:
    reg = build_default_activities()
    for ref in (
        "document.parse_excel",
        "document.parse_json",
        "document.parse_pdf",
        "document.write_excel",
        "document.merge_files",
    ):
        assert ref in reg, ref


# ---------- parse_excel ----------------------------------------------------


@pytest.mark.asyncio
async def test_parse_excel_default_sheet(tmp_path: Path) -> None:
    activities = build_default_activities()
    actx = _actx(tmp_path)
    uri = _put_xlsx(actx, "in.xlsx", [{"name": "Alice", "age": 30}, {"name": "Bob", "age": 42}])

    handler = activities.get("document.parse_excel")
    assert handler is not None
    out = await handler(actx, {"file_uri": uri})

    assert out["row_count"] == 2
    assert out["rows"] == [{"name": "Alice", "age": 30}, {"name": "Bob", "age": 42}]


@pytest.mark.asyncio
async def test_parse_excel_named_sheet(tmp_path: Path) -> None:
    import pandas as pd

    activities = build_default_activities()
    actx = _actx(tmp_path)

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame([{"x": 1}]).to_excel(writer, sheet_name="First", index=False)
        pd.DataFrame([{"y": 9}, {"y": 8}]).to_excel(writer, sheet_name="Second", index=False)
    uri = actx.object_store.put(str(actx.tenant_id), "multi.xlsx", buf.getvalue()).uri

    handler = activities.get("document.parse_excel")
    assert handler is not None
    out = await handler(actx, {"file_uri": uri, "sheet": "Second"})
    assert out["rows"] == [{"y": 9}, {"y": 8}]


# ---------- parse_json -----------------------------------------------------


@pytest.mark.asyncio
async def test_parse_json_from_uri(tmp_path: Path) -> None:
    activities = build_default_activities()
    actx = _actx(tmp_path)
    payload = {"data": {"items": [{"id": 1}, {"id": 2}]}}
    uri = actx.object_store.put(str(actx.tenant_id), "x.json", json.dumps(payload).encode()).uri

    handler = activities.get("document.parse_json")
    assert handler is not None
    out = await handler(actx, {"file_uri": uri})
    assert out["value"] == payload


@pytest.mark.asyncio
async def test_parse_json_dotted_path(tmp_path: Path) -> None:
    activities = build_default_activities()
    actx = _actx(tmp_path)
    handler = activities.get("document.parse_json")
    assert handler is not None
    text = json.dumps({"data": {"items": [{"id": 1}, {"id": 7}]}})
    out = await handler(actx, {"text": text, "path": "data.items.1.id"})
    assert out["value"] == 7


@pytest.mark.asyncio
async def test_parse_json_requires_source(tmp_path: Path) -> None:
    activities = build_default_activities()
    actx = _actx(tmp_path)
    handler = activities.get("document.parse_json")
    assert handler is not None
    with pytest.raises(ValueError, match="file_uri.*text|text"):
        await handler(actx, {})


# ---------- parse_pdf ------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_pdf_text_all_pages(tmp_path: Path) -> None:
    activities = build_default_activities()
    actx = _actx(tmp_path)
    pdf_bytes = _make_pdf(["hello page one", "second page here"])
    uri = actx.object_store.put(str(actx.tenant_id), "doc.pdf", pdf_bytes).uri

    handler = activities.get("document.parse_pdf")
    assert handler is not None
    out = await handler(actx, {"file_uri": uri})
    assert out["page_count"] == 2
    assert len(out["pages"]) == 2
    assert "hello page one" in out["text"]
    assert "second page here" in out["text"]


@pytest.mark.asyncio
async def test_parse_pdf_page_range(tmp_path: Path) -> None:
    activities = build_default_activities()
    actx = _actx(tmp_path)
    pdf_bytes = _make_pdf(["page A", "page B", "page C"])
    uri = actx.object_store.put(str(actx.tenant_id), "doc.pdf", pdf_bytes).uri

    handler = activities.get("document.parse_pdf")
    assert handler is not None
    out = await handler(actx, {"file_uri": uri, "page_start": 2, "page_end": 2})
    assert len(out["pages"]) == 1
    assert out["pages"][0]["page"] == 2
    assert "page B" in out["pages"][0]["text"]
    assert "page A" not in out["text"]


@pytest.mark.asyncio
async def test_parse_pdf_extract_tables(tmp_path: Path) -> None:
    activities = build_default_activities()
    actx = _actx(tmp_path)
    pdf_bytes = _make_pdf(["alpha    beta    gamma"])
    uri = actx.object_store.put(str(actx.tenant_id), "doc.pdf", pdf_bytes).uri

    handler = activities.get("document.parse_pdf")
    assert handler is not None
    out = await handler(actx, {"file_uri": uri, "extract_tables": True})
    tables = out["pages"][0]["tables"]
    assert tables and tables[0] == ["alpha", "beta", "gamma"]


# ---------- write_excel ----------------------------------------------------


@pytest.mark.asyncio
async def test_write_excel_round_trip(tmp_path: Path) -> None:
    activities = build_default_activities()
    actx = _actx(tmp_path)
    target = make_uri(str(actx.tenant_id), "out.xlsx")
    rows = [{"x": 1, "y": "a"}, {"x": 2, "y": "b"}]

    writer = activities.get("document.write_excel")
    parser = activities.get("document.parse_excel")
    assert writer is not None and parser is not None

    out = await writer(actx, {"file_uri": target, "rows": rows, "sheet_name": "Data"})
    assert out["file_uri"] == target
    assert out["row_count"] == 2

    back = await parser(actx, {"file_uri": target, "sheet": "Data"})
    assert back["rows"] == rows


@pytest.mark.asyncio
async def test_write_excel_unioned_columns(tmp_path: Path) -> None:
    activities = build_default_activities()
    actx = _actx(tmp_path)
    target = make_uri(str(actx.tenant_id), "ragged.xlsx")
    rows = [{"a": 1}, {"a": 2, "b": 3}]

    writer = activities.get("document.write_excel")
    parser = activities.get("document.parse_excel")
    assert writer is not None and parser is not None

    await writer(actx, {"file_uri": target, "rows": rows})
    back = await parser(actx, {"file_uri": target})
    assert back["rows"][0] == {"a": 1, "b": None}
    assert back["rows"][1] == {"a": 2, "b": 3}


# ---------- merge_files ----------------------------------------------------


@pytest.mark.asyncio
async def test_merge_files_csv(tmp_path: Path) -> None:
    activities = build_default_activities()
    actx = _actx(tmp_path)
    a = _put_csv(actx, "a.csv", "id,v\n1,x\n2,y\n")
    b = _put_csv(actx, "b.csv", "id,v\n3,z\n")
    out_uri = make_uri(str(actx.tenant_id), "merged.csv")

    handler = activities.get("document.merge_files")
    assert handler is not None
    out = await handler(actx, {"file_uris": [a, b], "output_uri": out_uri})
    assert out["row_count"] == 3

    merged_text = actx.object_store.get(out_uri).decode()
    assert "1,x" in merged_text and "3,z" in merged_text


@pytest.mark.asyncio
async def test_merge_files_excel_output(tmp_path: Path) -> None:
    activities = build_default_activities()
    actx = _actx(tmp_path)
    a = _put_xlsx(actx, "a.xlsx", [{"id": 1}, {"id": 2}])
    b = _put_xlsx(actx, "b.xlsx", [{"id": 3}])
    out_uri = make_uri(str(actx.tenant_id), "merged.xlsx")

    merge = activities.get("document.merge_files")
    parse = activities.get("document.parse_excel")
    assert merge is not None and parse is not None

    out = await merge(actx, {"file_uris": [a, b], "output_uri": out_uri})
    assert out["row_count"] == 3
    back = await parse(actx, {"file_uri": out_uri})
    assert [r["id"] for r in back["rows"]] == [1, 2, 3]


@pytest.mark.asyncio
async def test_merge_files_empty_list_raises(tmp_path: Path) -> None:
    activities = build_default_activities()
    actx = _actx(tmp_path)
    handler = activities.get("document.merge_files")
    assert handler is not None
    with pytest.raises(ValueError, match="at least one"):
        await handler(
            actx,
            {"file_uris": [], "output_uri": make_uri(str(actx.tenant_id), "x.csv")},
        )
