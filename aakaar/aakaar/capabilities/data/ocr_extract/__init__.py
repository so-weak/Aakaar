"""cap.ocr_extract — OCR an image stored in object storage.

Reads an image object (an `aakaar://` URI) from `ctx.object_store`, runs
optical character recognition over it with Tesseract (via pytesseract),
and returns the recognised plain text.

This is a deterministic, read-only data capability: it takes no
credentials and never touches the network. The heavy/optional bits —
`pytesseract` (the Python binding) and the `tesseract` binary itself —
are imported/located LAZILY inside the handler so importing this module
never fails on a host that lacks them. If either is missing the handler
raises a clear RuntimeError telling the operator what to install.

Inputs:
  source: required. An `aakaar://` object URI pointing at an image
          (PNG/JPEG/TIFF/etc. — anything Pillow can open).
  lang:   optional, default "eng". The Tesseract language pack(s) to use,
          e.g. "eng", "eng+deu". The matching traineddata must already be
          installed for the local tesseract.

Output:
  text:   the extracted text (may be empty if the image has no legible
          text — that is not an error).
"""

from __future__ import annotations

import io
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import CapabilityDefinition

logger = logging.getLogger(__name__)
CAP_REF = "cap.ocr_extract"


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(
        description=(
            "Object-store URI (aakaar://t/{tenant}/{key}) of the image to OCR. "
            "Any raster image Pillow can decode is accepted."
        )
    )
    lang: str = Field(
        default="eng",
        description=(
            "Tesseract language code(s), e.g. 'eng' or 'eng+deu'. The matching "
            "traineddata must be installed for the local tesseract."
        ),
    )


class _Outputs(BaseModel):
    text: str = Field(description="Plain text extracted from the image (may be empty).")


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "OCR an image stored in object storage and return its text. Reads the "
        "image from an aakaar:// URI and runs Tesseract over it via pytesseract. "
        "No credentials, no network. Requires the tesseract binary and the "
        "pytesseract package on the worker host."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("data", "ocr", "image", "text"),
)


def _require_pytesseract() -> Any:
    """Import pytesseract lazily, raising a clear RuntimeError if absent."""
    try:
        import pytesseract  # noqa: PLC0415  (lazy by design)
    except ImportError as e:
        raise RuntimeError(
            "cap.ocr_extract requires the 'pytesseract' package, which is not "
            "installed on this worker host. Install it with `pip install pytesseract`."
        ) from e
    return pytesseract


def _assert_tesseract_binary(pytesseract: Any) -> None:
    """Verify the tesseract binary is reachable; clear RuntimeError if not.

    pytesseract shells out to the `tesseract` executable. When it cannot be
    found pytesseract raises TesseractNotFoundError on first use; we probe
    here so the failure message is actionable rather than opaque.
    """
    try:
        pytesseract.get_tesseract_version()
    except Exception as e:  # TesseractNotFoundError (and friends)
        raise RuntimeError(
            "cap.ocr_extract requires the 'tesseract' OCR binary, which could "
            "not be found or run on this worker host. Install the tesseract-ocr "
            "engine and ensure it is on PATH."
        ) from e


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    source = inputs["source"]
    lang = (inputs.get("lang") or "eng").strip() or "eng"

    pytesseract = _require_pytesseract()
    _assert_tesseract_binary(pytesseract)

    from PIL import Image  # noqa: PLC0415  (Pillow is installed; kept lazy for symmetry)

    logger.info(
        "cap.ocr_extract start run_id=%s source=%s lang=%s",
        ctx.run_id,
        source,
        lang,
    )

    data = ctx.object_store.get(source)
    try:
        with Image.open(io.BytesIO(data)) as img:
            text = pytesseract.image_to_string(img, lang=lang)
    except Exception as e:
        # Bad/undecodable image, or a missing language pack -> surface clearly.
        raise RuntimeError(
            f"cap.ocr_extract: failed to OCR {source!r} (lang={lang!r}): {e}"
        ) from e

    text = (text or "").strip()
    logger.info(
        "cap.ocr_extract ok run_id=%s source=%s chars=%d",
        ctx.run_id,
        source,
        len(text),
    )
    return {"text": text}
