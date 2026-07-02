"""cap.micr_read — OCR the MICR (E-13B) code line at the bottom of a cheque.

Reads an image from managed storage (``aakaar://`` URI), crops the bottom strip,
runs the ported multi-variant consensus OCR (``aakaar_caps.cheque.micr.run_micr_ocr``),
and returns the union of unique text runs plus the parsed CTS layout
(cheque_no / city / bank / branch / tc) and the enhancement variants that were tried.

The heavy deps (``rapidocr`` / ``cv2`` / ``numpy``) are imported lazily inside the
pipeline; the executing host (agent) must have the optional ``cheque`` extra.
``run_micr_ocr`` never raises — on any setup error it returns empty ``text`` /
``parsed``. Read-only (``side_effecting=False``).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar_caps.cheque import micr
from aakaar_caps.cheque._serialize import to_jsonsafe
from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.micr_read"


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_uri: str = Field(description="Managed-storage URI (aakaar://...) of the cheque image to read the MICR line from.")
    bottom_fraction: float = Field(
        default=0.18, gt=0.0, le=1.0,
        description="Fraction of the image height, measured from the bottom, to crop as the MICR strip.",
    )
    upscale: int = Field(
        default=3, ge=1, le=8,
        description="Integer upscale factor applied to the strip before OCR (sharpens the small E-13B font).",
    )


class _Outputs(BaseModel):
    text: str = Field(description="Newline-joined union of every unique MICR text run across enhancement variants.")
    parsed: dict[str, Any] = Field(description="Structured CTS layout parsed from the strip (cheque_no / city / bank / branch / tc).")
    variants_tried: list[str] = Field(description="Names of the enhancement variants that were OCR'd (diagnostic).")


SPEC = CapabilitySpec(
    ref=CAP_REF,
    description=(
        "OCR the MICR (E-13B) code line at the bottom of a cheque image (from managed storage): crop the "
        "bottom strip, run multi-variant consensus OCR, and return the union of unique text runs plus the "
        "parsed CTS layout (cheque number, city, bank, branch, transaction code) and the enhancement "
        "variants tried. Offline, CPU. Read-only."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("ocr", "cheque", "banking"),
    side_effecting=False,
)


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    image_uri = inputs["image_uri"]
    bottom_fraction = float(inputs.get("bottom_fraction", 0.18))
    upscale = int(inputs.get("upscale", 3))

    data = await ctx.read_object(image_uri)
    logger.info("cap.micr_read start run_id=%s uri=%s bottom_fraction=%.3f upscale=%d",
                ctx.run_id, image_uri, bottom_fraction, upscale)

    result = micr.run_micr_ocr(data, bottom_fraction=bottom_fraction, upscale=upscale)
    out = {
        "text": result.text,
        "parsed": to_jsonsafe(result.parsed),
        "variants_tried": list(result.variants_tried),
    }
    logger.info("cap.micr_read ok run_id=%s chars=%d variants=%d parsed_keys=%s",
                ctx.run_id, len(result.text), len(result.variants_tried), sorted(out["parsed"].keys()))
    return out
