"""cap.signature_detect — detect whether a signature is present on a cheque.

Reads an image from managed storage (``aakaar://`` URI), runs the ported
signature-presence detector (``aakaar_caps.cheque.signature_detector.detect_signature``)
over the drawee-signature panel, and returns the boolean ``present`` verdict, the
measured ink ``density`` (0..1), and the 3-state ``verdict`` ("present" / "maybe" /
"absent"). The cropped ``region_png`` bytes the detector produces are DROPPED — a
cap never returns raw bytes.

The heavy deps (``cv2`` / ``numpy``) are imported lazily inside the detector; the
executing host (agent) must have the optional ``cheque`` extra. ``detect_signature``
never raises — when OpenCV is absent it returns ``present=False`` / ``verdict="absent"``.
Read-only (``side_effecting=False``).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar_caps.cheque import signature_detector
from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.signature_detect"


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_uri: str = Field(description="Managed-storage URI (aakaar://...) of the cheque image to check for a signature.")


class _Outputs(BaseModel):
    present: bool = Field(description="True iff the ink density crossed the presence threshold.")
    density: float = Field(description="Measured dark-pixel (ink) fraction in the signature panel [0,1].")
    verdict: str = Field(description="Three-state presence verdict: 'present' / 'maybe' / 'absent'.")


SPEC = CapabilitySpec(
    ref=CAP_REF,
    description=(
        "Detect whether a signature is present in the drawee-signature panel of a cheque image (from "
        "managed storage) by measuring ink density: returns the boolean presence verdict, the measured "
        "ink density (0..1), and a three-state verdict ('present' / 'maybe' / 'absent'). The cropped "
        "signature-panel image is not returned (no raw bytes). Offline, CPU. Read-only."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("ocr", "cheque", "banking"),
    side_effecting=False,
)


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    image_uri = inputs["image_uri"]

    data = await ctx.read_object(image_uri)
    logger.info("cap.signature_detect start run_id=%s uri=%s bytes=%d",
                ctx.run_id, image_uri, len(data))

    result = signature_detector.detect_signature(data)
    # Return only the JSON-safe verdict fields — DROP region_png (raw bytes).
    out = {
        "present": bool(result.present),
        "density": float(result.density),
        "verdict": result.verdict,
    }
    logger.info("cap.signature_detect ok run_id=%s present=%s density=%.4f verdict=%s missing_dep=%s",
                ctx.run_id, out["present"], out["density"], out["verdict"], result.missing_dep)
    return out
