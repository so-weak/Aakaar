"""cap.value_decision — accept/reject an OCR'd value by its heuristic confidence.

The gate is the OCR **heuristic confidence**: accept iff confidence >= threshold.
Nothing else blocks it — a confident read passes even if it isn't an exact match
to the recorded value (OCR can drop or misread a digit). The truth comparison is
still computed and reported (``match`` exact, ``similarity`` digit-closeness) for
the audit trail, but it does NOT gate the decision. The threshold comes from the
env var ``AAKAAR_OCR_ACCEPT_THRESHOLD`` unless overridden on the node. Emits
``decision`` + ``click_text`` so the DAG can feed it into ``cap.web_click`` (no
DAG branching). Pure logic, read-only.

Decision rule: ``accept = confidence >= threshold``.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.value_decision"
_ENV_THRESHOLD = "AAKAAR_OCR_ACCEPT_THRESHOLD"
_DEFAULT_THRESHOLD = 0.50


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extracted: str = Field(description="The OCR-extracted value (e.g. account number).")
    truth: str = Field(description="The reference/truth value read from the page (for reporting only).")
    confidence: float = Field(default=0.0,
        description="OCR heuristic confidence [0,1] — THIS is the gate: accept iff it >= threshold.")
    threshold: float | None = Field(
        default=None,
        description=(f"Minimum heuristic confidence to accept [0,1]. If omitted, read from "
                     f"${_ENV_THRESHOLD} (default {_DEFAULT_THRESHOLD})."),
    )
    accept_label: str = Field(default="Accept", description="Button label to click when accepted.")
    reject_label: str = Field(default="Reject", description="Button label to click when rejected.")
    digits_only: bool = Field(default=True, description="Compare only digits when reporting match/similarity.")


class _Outputs(BaseModel):
    decision: str = Field(description="'accept' or 'reject'.")
    click_text: str = Field(description="The button label to click (accept_label / reject_label).")
    match: bool = Field(description="Whether extracted == truth exactly (reported, not the gate).")
    threshold_used: float = Field(description="The confidence threshold actually applied.")
    similarity: float = Field(description="Digit similarity to the truth [0,1] (reported, not the gate).")


SPEC = CapabilitySpec(
    ref=CAP_REF,
    description=(
        "Decide accept vs reject for an OCR'd value by its heuristic confidence: accept iff "
        "confidence >= threshold (threshold from AAKAAR_OCR_ACCEPT_THRESHOLD env unless overridden). "
        "Exact match is not required and does not gate; match and digit-similarity to the truth are "
        "reported for audit. Emits the button label to click. Pure logic, read-only."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("decision", "ocr"),
    side_effecting=False,
)


def _norm(s: str, digits_only: bool) -> str:
    s = str(s or "")
    return re.sub(r"\D", "", s) if digits_only else s.strip().upper()


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _similarity(a: str, b: str) -> float:
    if not a and not b:
        return 0.0
    denom = max(len(a), len(b), 1)
    return round(1.0 - _levenshtein(a, b) / denom, 4)


def _resolve_threshold(inp: dict[str, Any]) -> float:
    if inp.get("threshold") is not None:
        return float(inp["threshold"])
    raw = os.getenv(_ENV_THRESHOLD)
    if raw:
        try:
            return float(raw)
        except ValueError:
            logger.warning("invalid %s=%r; using default %.2f", _ENV_THRESHOLD, raw, _DEFAULT_THRESHOLD)
    return _DEFAULT_THRESHOLD


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    digits_only = bool(inputs.get("digits_only", True))
    ex = _norm(inputs.get("extracted", ""), digits_only)
    tr = _norm(inputs.get("truth", ""), digits_only)
    conf = float(inputs.get("confidence", 0.0))
    threshold = _resolve_threshold(inputs)
    accept_label = str(inputs.get("accept_label", "Accept"))
    reject_label = str(inputs.get("reject_label", "Reject"))

    # The gate IS the heuristic confidence: confidence >= threshold -> accept.
    accept = conf >= threshold
    decision = "accept" if accept else "reject"
    out = {
        "decision": decision,
        "click_text": accept_label if accept else reject_label,
        "match": bool(ex) and ex == tr,
        "threshold_used": round(threshold, 4),
        "similarity": _similarity(ex, tr),
    }
    logger.info("cap.value_decision conf=%.4f thr=%.4f -> %s (extracted=%r truth=%r match=%s)",
                conf, threshold, decision, ex, tr, out["match"])
    return out
