"""cap.cheque_extract — OCR one side of a CTS cheque image and return its fields.

Reads an image from managed storage (``aakaar://`` URI), runs the ported CTS
vision pipeline (``aakaar_caps.cheque.cheque_ocr.extract_fields``) on the given
side, and returns the extracted ``ChequeFields`` as a JSON-safe dict (beneficiary,
cheque_no, amount, amount_words, account_no, MICR text, signature verdict, raw OCR
text, per-region reads, consensus + cross-field findings, and any missing-dep /
error diagnostics).

The heavy OCR deps (``rapidocr`` / ``cv2`` / ``numpy``) are imported lazily deep
inside the pipeline, so the server can register this capability without them; the
host that EXECUTES it (the agent) must have the optional ``cheque`` extra
installed. ``extract_fields`` never raises — inspect ``missing_dep`` / ``error``
in the returned dict. Read-only (``side_effecting=False``).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aakaar_caps.cheque import cheque_ocr
from aakaar_caps.cheque._serialize import to_jsonsafe
from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.cheque_extract"


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_uri: str = Field(description="Managed-storage URI (aakaar://...) of the cheque image to OCR.")
    side: Literal["front", "back"] = Field(
        default="front",
        description="Which cheque side this image is — 'front' also runs the MICR strip + signature passes.",
    )
    dom: dict[str, Any] | None = Field(
        default=None,
        description="Optional parsed bank-panel fields (DOM), used to hint the back-side account-number picker.",
    )


class _Outputs(BaseModel):
    fields: dict[str, Any] = Field(description="Extracted ChequeFields as a JSON-safe dict (see cheque_ocr.ChequeFields).")


SPEC = CapabilitySpec(
    ref=CAP_REF,
    description=(
        "OCR one side of a CTS cheque image (from managed storage) with the RapidOCR pipeline and "
        "return the extracted fields as a JSON-safe dict: beneficiary, cheque number, amount (figures + "
        "words), account number, MICR text, signature verdict, raw OCR text, per-region reads, consensus "
        "and cross-field findings, plus missing-dependency / error diagnostics. The 'front' side also runs "
        "the MICR strip and signature-presence passes. Offline, CPU. Read-only."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("ocr", "cheque", "banking"),
    side_effecting=False,
)


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    image_uri = inputs["image_uri"]
    side = inputs.get("side", "front")
    dom = inputs.get("dom")

    data = await ctx.read_object(image_uri)
    logger.info("cap.cheque_extract start run_id=%s uri=%s side=%s bytes=%d",
                ctx.run_id, image_uri, side, len(data))

    fields = cheque_ocr.extract_fields(data, side=side, dom=dom)
    out = {"fields": to_jsonsafe(fields)}
    logger.info("cap.cheque_extract ok run_id=%s side=%s missing_dep=%s error=%s",
                ctx.run_id, side, fields.missing_dep, fields.error)
    return out
