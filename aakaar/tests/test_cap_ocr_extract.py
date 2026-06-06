"""Tests for cap.ocr_extract.

The OCR happy path needs both the `pytesseract` package and the
`tesseract` binary on PATH; neither is guaranteed in CI, so that test
is gated behind importorskip + shutil.which and otherwise generates a
tiny image to recognise. Definition shape, input validation, and the
lazy-dependency error helpers are always exercised.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from aakaar.capabilities.data.ocr_extract import (
    CAP_REF,
    _assert_tesseract_binary,
    _require_pytesseract,
    definition,
    handler,
)
from aakaar.interpreter.activities.types import ActivityContext


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


# --------------------------------------------------------------------------
# Definition + input validation (no external deps)
# --------------------------------------------------------------------------


def test_definition_shape() -> None:
    assert definition.ref == CAP_REF == "cap.ocr_extract"
    assert definition.secrets == ()
    assert "ocr" in definition.tags
    assert set(definition.output_schema.model_fields) == {"text"}


def test_input_schema_defaults_lang_to_eng() -> None:
    parsed = definition.input_schema(source="aakaar://t/x/y.png")
    assert parsed.lang == "eng"


def test_input_schema_forbids_extra_keys() -> None:
    with pytest.raises(ValidationError):
        definition.input_schema(source="aakaar://t/x/y.png", bogus=1)


def test_input_schema_requires_source() -> None:
    with pytest.raises(ValidationError):
        definition.input_schema(lang="eng")


def test_require_pytesseract_message_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the lazy import to fail and assert the actionable RuntimeError."""
    import builtins

    real_import = builtins.__import__

    def _fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "pytesseract":
            raise ImportError("no module named pytesseract")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    with pytest.raises(RuntimeError, match="pytesseract"):
        _require_pytesseract()


def test_assert_tesseract_binary_message_when_missing() -> None:
    class _FakePT:
        def get_tesseract_version(self) -> str:
            raise OSError("tesseract is not installed or it's not in your PATH")

    with pytest.raises(RuntimeError, match="tesseract"):
        _assert_tesseract_binary(_FakePT())


# --------------------------------------------------------------------------
# Real OCR happy path (skipped if pytesseract / tesseract unavailable)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ocr_extract_happy_path(tmp_path: Path) -> None:
    pytest.importorskip("pytesseract")
    if shutil.which("tesseract") is None:
        pytest.skip("tesseract binary not installed")

    from PIL import Image, ImageDraw

    # Generate a tiny high-contrast image with clear, large text.
    img = Image.new("RGB", (320, 90), color="white")
    draw = ImageDraw.Draw(img)
    expected = "HELLO"
    # Default bitmap font is small; scale up so OCR has a fighting chance.
    draw.text((10, 30), expected, fill="black")
    img = img.resize((960, 270), Image.LANCZOS)

    png_path = tmp_path / "sample.png"
    img.save(png_path)

    ctx = _ctx(tmp_path)
    stored = ctx.object_store.put_file(
        str(ctx.tenant_id), "ocr/sample.png", png_path
    )

    out = await handler(ctx, {"source": stored.uri})

    assert isinstance(out["text"], str)
    # OCR on a synthetic bitmap-font image can be imperfect; require that the
    # recognised text is non-empty and that most of the expected glyphs appear.
    recognised = out["text"].upper().replace(" ", "")
    assert recognised, "expected some text to be recognised"
    hits = sum(1 for ch in expected if ch in recognised)
    assert hits >= 3, f"expected to recognise most of {expected!r}, got {out['text']!r}"


@pytest.mark.asyncio
async def test_ocr_extract_blank_image_returns_empty(tmp_path: Path) -> None:
    pytest.importorskip("pytesseract")
    if shutil.which("tesseract") is None:
        pytest.skip("tesseract binary not installed")

    from PIL import Image

    blank = tmp_path / "blank.png"
    Image.new("RGB", (64, 64), color="white").save(blank)

    ctx = _ctx(tmp_path)
    stored = ctx.object_store.put_file(str(ctx.tenant_id), "ocr/blank.png", blank)

    out = await handler(ctx, {"source": stored.uri})
    assert out["text"] == ""
