"""cap.cheque_verify — end-to-end CTS cheque verification (extract → validate → decide).

Convenience cap that runs the full ported pipeline over one cheque:

  1. OCR the front image (and the back image, when given) via
     ``cheque_ocr.extract_fields``.
  2. Run all validation rules via ``cheque_validation.validate_cheque`` (payee /
     amount figures-vs-words / date validity / account number / signature / MICR).
  3. Compute the operator recommendation via ``cheque_decision.decide``.

Returns a flat, JSON-safe verdict: the decision status + human summary + pre-filled
rejection reason, the report's overall status, the per-rule ``checks`` list, and the
extracted ``fields`` for both sides (for audit). The heavy OCR deps
(``rapidocr`` / ``cv2`` / ``numpy``) are imported lazily inside the pipeline; the
executing host (agent) must have the optional ``cheque`` extra. Read-only
(``side_effecting=False``).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar_caps.cheque import cheque_decision, cheque_ocr, cheque_validation
from aakaar_caps.cheque._serialize import to_jsonsafe
from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.cheque_verify"


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    front_image_uri: str = Field(description="Managed-storage URI (aakaar://...) of the cheque FRONT image.")
    back_image_uri: str | None = Field(
        default=None,
        description="Optional managed-storage URI of the cheque BACK image (endorsement side).",
    )
    dom: dict[str, Any] | None = Field(
        default=None,
        description="Optional parsed bank-panel fields (DOM), fed to the validator for cross-checks.",
    )
    validity_days: int = Field(
        default=90,
        description="Cheque validity window in days from the written date (RBI default = 90).",
    )


class _Outputs(BaseModel):
    status: str = Field(description="Decision headline: AUTO_APPROVE / AUTO_REJECT / NEEDS_REVIEW.")
    summary: str = Field(description="One human sentence the operator reads at a glance.")
    rejection_reason: str = Field(description="Pre-filled rejection reason (set only when status == AUTO_REJECT).")
    overall_status: str = Field(description="The validation report's overall status: ACCEPT / REVIEW / REJECT.")
    checks: list[dict[str, Any]] = Field(description="Per-rule outcomes (one CheckResult dict per validation rule).")
    fields: dict[str, Any] = Field(description="Extracted fields per side: {'front': {...}, 'back': {...}|None}.")


SPEC = CapabilitySpec(
    ref=CAP_REF,
    description=(
        "End-to-end CTS cheque verification from managed-storage images: OCR the front (and optional "
        "back) with the RapidOCR pipeline, run all validation rules (payee, amount figures-vs-words, "
        "date validity, account number, signature presence, MICR), and compute the operator "
        "recommendation. Returns a JSON-safe verdict — decision status (AUTO_APPROVE / AUTO_REJECT / "
        "NEEDS_REVIEW), a human summary, a pre-filled rejection reason, the report's overall status, the "
        "per-rule checks, and the extracted fields for both sides. Offline, CPU. Read-only."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("ocr", "cheque", "banking"),
    side_effecting=False,
)


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    front_uri = inputs["front_image_uri"]
    back_uri = inputs.get("back_image_uri")
    dom = inputs.get("dom")
    validity_days = int(inputs.get("validity_days", 90))

    logger.info("cap.cheque_verify start run_id=%s front=%s back=%s validity_days=%d",
                ctx.run_id, front_uri, back_uri, validity_days)

    front_data = await ctx.read_object(front_uri)
    front_fields = cheque_ocr.extract_fields(front_data, side="front", dom=dom)

    back_fields = None
    if back_uri:
        back_data = await ctx.read_object(back_uri)
        back_fields = cheque_ocr.extract_fields(back_data, side="back", dom=dom)

    report = cheque_validation.validate_cheque(
        front=front_fields,
        back=back_fields,
        dom=dom,
        validity_days=validity_days,
    )
    decision = cheque_decision.decide(report)

    out = {
        "status": decision.status,
        "summary": decision.summary,
        "rejection_reason": decision.rejection_reason,
        "overall_status": report.overall_status,
        "checks": to_jsonsafe(report.checks),
        "fields": {
            "front": to_jsonsafe(front_fields),
            "back": to_jsonsafe(back_fields) if back_fields is not None else None,
        },
    }
    logger.info("cap.cheque_verify ok run_id=%s status=%s overall=%s checks=%d",
                ctx.run_id, decision.status, report.overall_status, len(report.checks))
    return out
