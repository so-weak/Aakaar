"""Tests for cap.image_convert.

Drives the handler with a hand-built ActivityContext + LocalFsObjectStore.
Real PNGs are created via Pillow, written into tenant storage as aakaar://
URIs, transformed, and read back to assert the result dimensions/format.

Covers:
  - resize (explicit dims + aspect-preserving single dim)
  - grayscale -> mode 'L'
  - crop, rotate (expand), thumbnail, convert (format change + alpha flatten)
  - definition shape + input-schema validation
  - pure helpers (_ext_from_uri, _resolve_format, _scaled_dim, _as_int)
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from aakaar.capabilities.data.image_convert import (
    CAP_REF,
    _as_int,
    _ext_from_uri,
    _resolve_format,
    _scaled_dim,
    definition,
    handler,
)
from aakaar.interpreter.activities.types import ActivityContext

PIL = pytest.importorskip("PIL")
from PIL import Image as PILImage  # noqa: E402


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


def _put_png(ctx: ActivityContext, key: str, size: tuple[int, int], color: str = "red", mode: str = "RGB") -> str:
    img = PILImage.new(mode, size, color if mode != "L" else 128)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return ctx.object_store.put(str(ctx.tenant_id), key, buf.getvalue()).uri


def _open_result(ctx: ActivityContext, uri: str) -> PILImage.Image:
    raw = ctx.object_store.get(uri)
    return PILImage.open(io.BytesIO(raw))


# --------------------------------------------------------------------------
# Happy paths
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resize_explicit_dims(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = _put_png(ctx, "in.png", (40, 20))
    out = await handler(ctx, {"source": src, "op": "resize", "params": {"width": 10, "height": 8}})
    assert out["width"] == 10
    assert out["height"] == 8
    assert out["format"] == "PNG"
    assert out["result_uri"].startswith("aakaar://t/")
    img = _open_result(ctx, out["result_uri"])
    assert img.size == (10, 8)


@pytest.mark.asyncio
async def test_resize_preserves_aspect_when_one_dim(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = _put_png(ctx, "in.png", (40, 20))
    out = await handler(ctx, {"source": src, "op": "resize", "params": {"width": 20}})
    assert out["width"] == 20
    assert out["height"] == 10  # aspect 2:1 preserved


@pytest.mark.asyncio
async def test_grayscale_yields_mode_l(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = _put_png(ctx, "in.png", (8, 8), color="blue")
    out = await handler(ctx, {"source": src, "op": "grayscale"})
    img = _open_result(ctx, out["result_uri"])
    assert img.mode == "L"
    assert img.size == (8, 8)


@pytest.mark.asyncio
async def test_resize_then_grayscale_pipeline(tmp_path: Path) -> None:
    """Spec's requested flow: resize then grayscale, chaining through storage."""
    ctx = _ctx(tmp_path)
    src = _put_png(ctx, "orig.png", (30, 30), color="green")
    resized = await handler(ctx, {"source": src, "op": "resize", "params": {"width": 15, "height": 15}})
    assert resized["width"] == 15 and resized["height"] == 15
    gray = await handler(ctx, {"source": resized["result_uri"], "op": "grayscale"})
    img = _open_result(ctx, gray["result_uri"])
    assert img.mode == "L"
    assert img.size == (15, 15)


@pytest.mark.asyncio
async def test_crop(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = _put_png(ctx, "in.png", (20, 20))
    out = await handler(
        ctx,
        {"source": src, "op": "crop", "params": {"left": 2, "top": 4, "right": 12, "bottom": 18}},
    )
    assert out["width"] == 10
    assert out["height"] == 14


@pytest.mark.asyncio
async def test_rotate_expand_grows_canvas(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = _put_png(ctx, "in.png", (10, 20))
    out = await handler(ctx, {"source": src, "op": "rotate", "params": {"degrees": 90}})
    # 90deg with expand swaps the dimensions.
    assert out["width"] == 20
    assert out["height"] == 10


@pytest.mark.asyncio
async def test_thumbnail_fits_box_preserving_aspect(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = _put_png(ctx, "in.png", (40, 20))
    out = await handler(ctx, {"source": src, "op": "thumbnail", "params": {"width": 10, "height": 10}})
    # Longest side fits the box; aspect preserved -> 10x5.
    assert out["width"] == 10
    assert out["height"] == 5


@pytest.mark.asyncio
async def test_convert_changes_format_to_jpeg(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = _put_png(ctx, "in.png", (8, 8), color="red")
    out = await handler(ctx, {"source": src, "op": "convert", "format": "JPEG", "quality": 80})
    assert out["format"] == "JPEG"
    assert out["result_uri"].endswith(".jpg")
    img = _open_result(ctx, out["result_uri"])
    assert img.format == "JPEG"


@pytest.mark.asyncio
async def test_rgba_flattened_for_jpeg(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = _put_png(ctx, "in.png", (8, 8), color=(0, 0, 0, 0), mode="RGBA")
    # Would raise inside Pillow if alpha weren't flattened first.
    out = await handler(ctx, {"source": src, "op": "convert", "format": "JPEG"})
    assert out["format"] == "JPEG"
    img = _open_result(ctx, out["result_uri"])
    assert img.mode == "RGB"


# --------------------------------------------------------------------------
# Validation / errors
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_op_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = _put_png(ctx, "in.png", (8, 8))
    with pytest.raises(ValueError, match="unsupported op"):
        await handler(ctx, {"source": src, "op": "sharpen"})


@pytest.mark.asyncio
async def test_resize_without_dims_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = _put_png(ctx, "in.png", (8, 8))
    with pytest.raises(ValueError, match="width and/or"):
        await handler(ctx, {"source": src, "op": "resize", "params": {}})


@pytest.mark.asyncio
async def test_crop_bad_box_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = _put_png(ctx, "in.png", (8, 8))
    with pytest.raises(ValueError, match="right>left"):
        await handler(
            ctx,
            {"source": src, "op": "crop", "params": {"left": 5, "top": 0, "right": 2, "bottom": 8}},
        )


@pytest.mark.asyncio
async def test_undecodable_source_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    bad = ctx.object_store.put(str(ctx.tenant_id), "in.png", b"not an image").uri
    with pytest.raises(RuntimeError, match="could not decode"):
        await handler(ctx, {"source": bad, "op": "grayscale"})


# --------------------------------------------------------------------------
# Security: pixel-count bomb guard
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_source_refused_before_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aakaar.capabilities.data.image_convert as mod

    ctx = _ctx(tmp_path)
    monkeypatch.setattr(mod, "_MAX_PIXELS", 100)
    src = _put_png(ctx, "in.png", (20, 20))  # 400 px > the patched 100-px cap
    with pytest.raises(RuntimeError, match="pixel limit"):
        await handler(ctx, {"source": src, "op": "grayscale"})


@pytest.mark.asyncio
async def test_resize_to_bomb_dimensions_refused(tmp_path: Path) -> None:
    # Real default limit: a 20000x20000 target (400M px) must be refused
    # before any allocation, even from a tiny source.
    ctx = _ctx(tmp_path)
    src = _put_png(ctx, "in.png", (8, 8))
    with pytest.raises(RuntimeError, match="pixel limit"):
        await handler(
            ctx,
            {"source": src, "op": "resize", "params": {"width": 20000, "height": 20000}},
        )


@pytest.mark.asyncio
async def test_crop_to_bomb_dimensions_refused(tmp_path: Path) -> None:
    # Crop boxes beyond the source pad with background, so a huge box is an
    # output-geometry bomb regardless of the source size.
    ctx = _ctx(tmp_path)
    src = _put_png(ctx, "in.png", (8, 8))
    with pytest.raises(RuntimeError, match="pixel limit"):
        await handler(
            ctx,
            {
                "source": src,
                "op": "crop",
                "params": {"left": 0, "top": 0, "right": 100_000, "bottom": 100_000},
            },
        )


# --------------------------------------------------------------------------
# Definition + input schema
# --------------------------------------------------------------------------


def test_definition_shape() -> None:
    assert definition.ref == CAP_REF == "cap.image_convert"
    assert definition.secrets == ()
    assert "image" in definition.tags
    assert definition.output_schema.model_fields.keys() >= {"result_uri", "width", "height", "format"}


def test_input_schema_forbids_extra_keys() -> None:
    with pytest.raises(ValidationError):
        definition.input_schema(source="aakaar://t/x/y.png", op="resize", bogus=1)


def test_input_schema_requires_source_and_op() -> None:
    with pytest.raises(ValidationError):
        definition.input_schema(op="resize")
    with pytest.raises(ValidationError):
        definition.input_schema(source="aakaar://t/x/y.png")


def test_input_schema_quality_bounds() -> None:
    with pytest.raises(ValidationError):
        definition.input_schema(source="aakaar://t/x/y.png", op="convert", quality=0)
    with pytest.raises(ValidationError):
        definition.input_schema(source="aakaar://t/x/y.png", op="convert", quality=101)


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def test_ext_from_uri() -> None:
    assert _ext_from_uri("aakaar://t/tenant-1/runs/r/in.PNG") == "png"
    assert _ext_from_uri("aakaar://t/tenant-1/noext") == ""


def test_resolve_format_override_and_default() -> None:
    assert _resolve_format("aakaar://t/x/in.png", "JPEG") == ("JPEG", "jpg")
    assert _resolve_format("aakaar://t/x/in.webp", None) == ("WEBP", "webp")
    assert _resolve_format("aakaar://t/x/in.bin", None) == ("PNG", "png")


def test_resolve_format_unsupported_raises() -> None:
    with pytest.raises(ValueError, match="unsupported format"):
        _resolve_format("aakaar://t/x/in.png", "svg")


def test_scaled_dim() -> None:
    # Explicit target wins.
    assert _scaled_dim(50, 100, None, 200) == 50
    # Derive from other dim, preserving aspect (200 src h -> 100 target = half).
    assert _scaled_dim(None, 100, 100, 200) == 50
    # No info -> keep source.
    assert _scaled_dim(None, 100, None, 200) == 100


def test_as_int() -> None:
    assert _as_int({"w": 5}, "w") == 5
    assert _as_int({}, "w") is None
    with pytest.raises(ValueError, match="must be an integer"):
        _as_int({"w": "abc"}, "w")
