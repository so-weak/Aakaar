"""cap.image_convert — image transforms over a stored object via Pillow.

Server-local, no-network, no-credential capability. Reads a single image
from the tenant's object store (an ``aakaar://`` URI the upstream graph
already produced — e.g. ``cap.file_download``, ``cap.screenshot``, or an
email attachment), applies one image operation, writes the result back to
the object store, and returns the new URI plus the result dimensions and
format.

Operations (``inputs.op``):
  - resize     params {width, height}. Either may be omitted, in which case
               the missing dimension is computed to preserve aspect ratio.
  - crop       params {left, top, right, bottom} — pixel box; right/bottom
               are exclusive (Pillow's box convention).
  - rotate     params {degrees} — counter-clockwise; ``expand`` (bool,
               default true) grows the canvas so corners aren't clipped.
  - convert    re-encode only (no geometry change); pair with ``format`` to
               change the container, or ``params.mode`` to change the pixel
               mode (e.g. "RGB", "L", "RGBA").
  - grayscale  convert to 8-bit luminance ("L").
  - thumbnail  params {width, height} — fit within the box, preserving
               aspect ratio (never upscales). Width/height default to a
               square box when only one is given.

Output container: ``inputs.format`` (e.g. "PNG", "JPEG", "WEBP") overrides
the encoding; when null the source extension decides, defaulting to PNG.
``inputs.quality`` (1-100) is forwarded to lossy encoders (JPEG/WEBP) and
ignored otherwise. Saving an image with alpha as JPEG flattens it onto a
white background (JPEG has no alpha channel).

Pillow is imported lazily inside the handler so importing this module never
fails when the library is absent; a clear RuntimeError is raised if Pillow
is unavailable at run time. No secrets, no network.
"""

from __future__ import annotations

import io
import logging
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import CapabilityDefinition

logger = logging.getLogger(__name__)
CAP_REF = "cap.image_convert"

_OPS = {"resize", "crop", "rotate", "convert", "grayscale", "thumbnail"}

# Maps requested/derived output formats to a canonical Pillow format name and
# the file extension we store under. Pillow keys off the format name, not the
# extension, so we normalise both here.
_FORMAT_ALIASES = {
    "png": ("PNG", "png"),
    "jpg": ("JPEG", "jpg"),
    "jpeg": ("JPEG", "jpg"),
    "webp": ("WEBP", "webp"),
    "gif": ("GIF", "gif"),
    "bmp": ("BMP", "bmp"),
    "tif": ("TIFF", "tif"),
    "tiff": ("TIFF", "tif"),
}

# Formats that carry no alpha channel; an RGBA/LA/P image must be flattened
# before saving to one of these or Pillow raises.
_NO_ALPHA_FORMATS = {"JPEG", "BMP"}


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(
        description="aakaar:// URI of the source image (produced upstream).",
    )
    op: str = Field(
        description=(
            "Operation to apply: 'resize', 'crop', 'rotate', 'convert', "
            "'grayscale', or 'thumbnail'."
        ),
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Operation parameters. resize/thumbnail: {width?, height?}; "
            "crop: {left, top, right, bottom}; rotate: {degrees, expand?}; "
            "convert: {mode?}. Unused for grayscale."
        ),
    )
    format: str | None = Field(
        default=None,
        description=(
            "Output container, e.g. 'PNG', 'JPEG', 'WEBP'. Null keeps the "
            "source format (defaulting to PNG when it can't be inferred)."
        ),
    )
    quality: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description=(
            "Encoder quality for lossy formats (JPEG/WEBP); 1-100. "
            "Ignored for lossless formats."
        ),
    )


class _Outputs(BaseModel):
    result_uri: str = Field(description="aakaar:// URI of the transformed image.")
    width: int = Field(description="Width of the result image in pixels.")
    height: int = Field(description="Height of the result image in pixels.")
    format: str = Field(description="Pillow format name of the result, e.g. 'PNG'.")


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Apply one image operation (resize, crop, rotate, convert, grayscale, "
        "or thumbnail) to a stored image using Pillow, write the result back to "
        "the object store, and return its URI and dimensions. Optionally "
        "re-encodes to a different format/quality. Lazy Pillow import; no "
        "secrets, no network."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("data", "image", "convert", "resize", "pillow"),
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _ext_from_uri(uri: str) -> str:
    """Return the lowercase extension of the URI key, or '' when absent."""
    key = uri.rsplit("/", 1)[-1]
    return key.rsplit(".", 1)[-1].lower() if "." in key else ""


def _resolve_format(source_uri: str, override: str | None) -> tuple[str, str]:
    """Resolve (pillow_format_name, file_extension) for the output.

    Honors an explicit override first, then the source extension, then PNG.
    """
    if override:
        key = override.strip().lower()
        if key not in _FORMAT_ALIASES:
            raise ValueError(
                f"cap.image_convert: unsupported format {override!r}; expected "
                f"one of {sorted({a.upper() for a in _FORMAT_ALIASES})}"
            )
        return _FORMAT_ALIASES[key]
    src_ext = _ext_from_uri(source_uri)
    if src_ext in _FORMAT_ALIASES:
        return _FORMAT_ALIASES[src_ext]
    return _FORMAT_ALIASES["png"]


def _as_int(params: dict[str, Any], name: str) -> int | None:
    """Read an optional non-negative int param; None when absent/null."""
    val = params.get(name)
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"cap.image_convert: param {name!r} must be an integer, got {val!r}"
        ) from e


def _scaled_dim(target: int | None, this_src: int, other_target: int | None, other_src: int) -> int:
    """Compute one output dimension, deriving it from the other to keep aspect."""
    if target is not None:
        return max(1, target)
    if other_target is not None and other_src:
        return max(1, round(this_src * (other_target / other_src)))
    return this_src


# ---------------------------------------------------------------------------
# Operations (each takes a PIL.Image and the params dict, returns a PIL.Image)
# ---------------------------------------------------------------------------


def _op_resize(img: Any, params: dict[str, Any]) -> Any:
    w = _as_int(params, "width")
    h = _as_int(params, "height")
    if w is None and h is None:
        raise ValueError(
            "cap.image_convert: resize needs params.width and/or params.height"
        )
    src_w, src_h = img.size
    out_w = _scaled_dim(w, src_w, h, src_h)
    out_h = _scaled_dim(h, src_h, w, src_w)
    from PIL import Image as PILImage

    return img.resize((out_w, out_h), PILImage.LANCZOS)


def _op_crop(img: Any, params: dict[str, Any]) -> Any:
    box = tuple(_as_int(params, k) for k in ("left", "top", "right", "bottom"))
    if any(v is None for v in box):
        raise ValueError(
            "cap.image_convert: crop needs params.left, top, right, bottom"
        )
    left, top, right, bottom = box  # type: ignore[misc]
    if right <= left or bottom <= top:  # type: ignore[operator]
        raise ValueError(
            "cap.image_convert: crop box must have right>left and bottom>top"
        )
    return img.crop((left, top, right, bottom))


def _op_rotate(img: Any, params: dict[str, Any]) -> Any:
    degrees_raw = params.get("degrees")
    if degrees_raw is None:
        raise ValueError("cap.image_convert: rotate needs params.degrees")
    try:
        degrees = float(degrees_raw)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"cap.image_convert: rotate degrees must be numeric, got {degrees_raw!r}"
        ) from e
    expand = bool(params.get("expand", True))
    return img.rotate(degrees, expand=expand)


def _op_convert(img: Any, params: dict[str, Any]) -> Any:
    mode = params.get("mode")
    if mode:
        return img.convert(str(mode))
    return img


def _op_grayscale(img: Any, _params: dict[str, Any]) -> Any:
    return img.convert("L")


def _op_thumbnail(img: Any, params: dict[str, Any]) -> Any:
    w = _as_int(params, "width")
    h = _as_int(params, "height")
    if w is None and h is None:
        raise ValueError(
            "cap.image_convert: thumbnail needs params.width and/or params.height"
        )
    box_w = w if w is not None else h
    box_h = h if h is not None else w
    from PIL import Image as PILImage

    out = img.copy()
    out.thumbnail((max(1, box_w), max(1, box_h)), PILImage.LANCZOS)  # type: ignore[arg-type]
    return out


_OP_FUNCS = {
    "resize": _op_resize,
    "crop": _op_crop,
    "rotate": _op_rotate,
    "convert": _op_convert,
    "grayscale": _op_grayscale,
    "thumbnail": _op_thumbnail,
}


def _flatten_for_no_alpha(img: Any) -> Any:
    """Drop alpha onto a white background for formats that can't store it."""
    from PIL import Image as PILImage

    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = PILImage.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    if img.mode not in ("RGB", "L"):
        return img.convert("RGB")
    return img


def _encode(img: Any, pil_format: str, quality: int | None) -> bytes:
    """Serialize a PIL image to bytes in the requested format."""
    if pil_format in _NO_ALPHA_FORMATS:
        img = _flatten_for_no_alpha(img)
    save_kwargs: dict[str, Any] = {}
    if quality is not None and pil_format in ("JPEG", "WEBP"):
        save_kwargs["quality"] = int(quality)
    buf = io.BytesIO()
    img.save(buf, format=pil_format, **save_kwargs)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    try:
        from PIL import Image as PILImage
    except Exception as e:  # pragma: no cover - Pillow is installed in v1
        raise RuntimeError(
            "cap.image_convert: Pillow (PIL) is required but is not installed"
        ) from e

    source = inputs["source"]
    op = str(inputs["op"]).strip().lower()
    if op not in _OPS:
        raise ValueError(
            f"cap.image_convert: unsupported op {inputs['op']!r}; expected one "
            f"of {sorted(_OPS)}"
        )
    params = inputs.get("params") or {}
    quality = inputs.get("quality")

    pil_format, ext = _resolve_format(source, inputs.get("format"))

    logger.info(
        "cap.image_convert start run_id=%s source=%s op=%s out_format=%s",
        ctx.run_id,
        source,
        op,
        pil_format,
    )

    raw = ctx.object_store.get(source)
    try:
        with PILImage.open(io.BytesIO(raw)) as opened:
            opened.load()
            img = opened.copy()
    except Exception as e:
        raise RuntimeError(
            f"cap.image_convert: could not decode image at {source!r}: {e}"
        ) from e

    img = _OP_FUNCS[op](img, params)
    out_bytes = _encode(img, pil_format, quality)
    width, height = img.size

    key = f"runs/{ctx.run_id}/images/{uuid.uuid4().hex}.{ext}"
    obj = ctx.object_store.put(str(ctx.tenant_id), key, out_bytes)

    logger.info(
        "cap.image_convert ok run_id=%s result_uri=%s op=%s size=%dx%d format=%s bytes=%d",
        ctx.run_id,
        obj.uri,
        op,
        width,
        height,
        pil_format,
        len(out_bytes),
    )
    return {
        "result_uri": obj.uri,
        "width": width,
        "height": height,
        "format": pil_format,
    }
