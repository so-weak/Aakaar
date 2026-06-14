"""Tests for cap.pdf_extract.

Drives the handler with a hand-built ActivityContext and a
LocalFsObjectStore. pypdf's ``add_blank_page`` can't emit text, so test
PDFs are hand-assembled (catalog/pages/font + one Helvetica text line per
page, with a correct xref table) — small enough to stay readable and real
enough for pypdf's text extractor. Covers:
  - whole-document and selector-driven extraction (text per page + joined)
  - the max_pages guard (truncates + flags instead of failing)
  - selector validation (out-of-range / malformed)
  - unreadable source bytes
  - graceful degradation when pypdf is missing
  - definition shape + input-schema validation
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from aakaar.capabilities.data.pdf_extract import CAP_REF, definition, handler
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


def _make_text_pdf(page_texts: list[str]) -> bytes:
    """Assemble a minimal PDF with one Helvetica text line per page.

    Object layout: 1=catalog, 2=pages, 3=font, then (page, contents) pairs.
    Offsets in the xref table are computed, so the file is well-formed.
    """
    n = len(page_texts)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, text in enumerate(page_texts):
        stream = f"BT /F1 12 Tf 50 100 Td ({text}) Tj ET".encode()
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
                f"/Resources << /Font << /F1 3 0 R >> >> "
                f"/Contents {5 + 2 * i} 0 R >>"
            ).encode()
        )
        objects.append(
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for idx, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{idx} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


def _put_pdf(ctx: ActivityContext, key: str, page_texts: list[str]) -> str:
    return ctx.object_store.put(
        str(ctx.tenant_id), key, _make_text_pdf(page_texts)
    ).uri


# --------------------------------------------------------------------------
# Happy paths
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_all_pages(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    uri = _put_pdf(ctx, "doc.pdf", ["alpha words", "beta words", "gamma words"])
    out = await handler(ctx, {"source": uri})
    assert out["page_count"] == 3
    assert out["truncated"] is False
    assert [p["page"] for p in out["pages"]] == [1, 2, 3]
    assert "alpha" in out["pages"][0]["text"]
    assert "gamma" in out["pages"][2]["text"]
    for word in ("alpha", "beta", "gamma"):
        assert word in out["text"]


@pytest.mark.asyncio
async def test_page_selector_orders_and_filters(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    uri = _put_pdf(ctx, "doc.pdf", ["one", "two", "three", "four"])
    out = await handler(ctx, {"source": uri, "pages": ["3-4", 1]})
    assert [p["page"] for p in out["pages"]] == [3, 4, 1]
    assert "two" not in out["text"]
    assert out["page_count"] == 4
    assert out["truncated"] is False


@pytest.mark.asyncio
async def test_max_pages_truncates_and_flags(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    uri = _put_pdf(ctx, "doc.pdf", ["p1", "p2", "p3", "p4"])
    out = await handler(ctx, {"source": uri, "max_pages": 2})
    assert out["truncated"] is True
    assert [p["page"] for p in out["pages"]] == [1, 2]
    assert out["page_count"] == 4
    assert "p3" not in out["text"]


# --------------------------------------------------------------------------
# Validation + failure modes
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_out_of_range_page_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    uri = _put_pdf(ctx, "doc.pdf", ["only"])
    with pytest.raises(RuntimeError, match="out of range"):
        await handler(ctx, {"source": uri, "pages": [9]})


@pytest.mark.asyncio
async def test_malformed_selector_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    uri = _put_pdf(ctx, "doc.pdf", ["only"])
    with pytest.raises(RuntimeError, match="malformed"):
        await handler(ctx, {"source": uri, "pages": ["abc"]})


@pytest.mark.asyncio
async def test_huge_range_selector_refused_without_blowup(tmp_path: Path) -> None:
    # A range string with an enormous upper bound is expanded inside
    # parse_page_selector *before* max_pages clamps the volume; without the
    # span guard list(range(...)) materializes billions of ints (tens of GB)
    # against a one-page document. It must be refused by arithmetic instead.
    ctx = _ctx(tmp_path)
    uri = _put_pdf(ctx, "doc.pdf", ["only"])
    with pytest.raises(RuntimeError, match="spans more than"):
        await handler(ctx, {"source": uri, "pages": ["1-2000000000"]})


@pytest.mark.asyncio
async def test_forged_page_count_does_not_materialize_full_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An encrypted PDF reports its page count from the untrusted /Pages /Count
    (pypdf trusts the hint on the encrypted path), so a forged 2-billion /Count
    must NOT make the all-pages path allocate list(range(page_count)). The build
    has to be bounded by max_pages."""
    import aakaar.capabilities.data.pdf_extract as mod

    forged_count = 2_000_000_000

    class _Page:
        def extract_text(self) -> str:
            return "x"

    class _Pages:
        def __len__(self) -> int:
            return forged_count

        def __getitem__(self, idx: int) -> _Page:
            # A real list(range(forged_count)) would never reach indexing; if the
            # handler tried, it would hang/OOM first. Cap defensively anyway.
            if idx >= 10_000:
                raise AssertionError("handler indexed beyond a sane bound")
            return _Page()

    class _Reader:
        is_encrypted = False
        pages = _Pages()

    monkeypatch.setattr(mod, "_reader_from_bytes", lambda raw: _Reader())
    uri = _put_pdf(ctx := _ctx(tmp_path), "doc.pdf", ["only"])
    out = await handler(ctx, {"source": uri, "max_pages": 5})
    # Bounded to max_pages, flagged truncated, page_count echoed honestly.
    assert len(out["pages"]) == 5
    assert out["truncated"] is True
    assert out["page_count"] == forged_count


@pytest.mark.asyncio
async def test_unreadable_pdf_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    uri = ctx.object_store.put(str(ctx.tenant_id), "bogus.pdf", b"not a pdf").uri
    with pytest.raises(RuntimeError, match="could not read PDF"):
        await handler(ctx, {"source": uri})


@pytest.mark.asyncio
async def test_missing_pypdf_degrades_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path)
    uri = _put_pdf(ctx, "doc.pdf", ["only"])
    # None in sys.modules makes `import pypdf` raise ImportError, simulating
    # a slim install without the 'doc' extra.
    monkeypatch.setitem(sys.modules, "pypdf", None)
    with pytest.raises(RuntimeError, match="'doc' extra"):
        await handler(ctx, {"source": uri})


# --------------------------------------------------------------------------
# Definition + input schema
# --------------------------------------------------------------------------


def test_definition_shape() -> None:
    assert definition.ref == CAP_REF == "cap.pdf_extract"
    assert definition.secrets == ()
    assert "pdf" in definition.tags


def test_input_schema_forbids_extra_keys() -> None:
    with pytest.raises(ValidationError):
        definition.input_schema(source="aakaar://t/x/a.pdf", bogus=1)


def test_input_schema_bounds_max_pages() -> None:
    with pytest.raises(ValidationError):
        definition.input_schema(source="aakaar://t/x/a.pdf", max_pages=0)
    with pytest.raises(ValidationError):
        definition.input_schema(source="aakaar://t/x/a.pdf", max_pages=10_000)
