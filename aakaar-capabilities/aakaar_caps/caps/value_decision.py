"""cap.value_decision — accept/reject an OCR'd value against a truth + threshold.

Compares the OCR-extracted account number to the truth value read from the page,
gated by a confidence threshold. The threshold comes from an env var
(``AAKAAR_OCR_ACCEPT_THRESHOLD``) unless overridden on the node — "threshold set
in the env". Emits ``decision`` ("accept"/"reject") and ``click_text`` ("Accept"/
"Reject") so the DAG can feed it straight into ``cap.web_click(text=...)`` — the
DAG has no conditional branching, so the decision picks the button label here.

Decision rule: accept iff the digit strings match AND heuristic confidence >=
threshold; otherwise reject. Pure logic — no browser, no I/O. Read-only.
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
_DEFAULT_THRESHOLD = 0.60


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extracted: str = Field(description="The OCR-extracted value (e.g. account number).")
    truth: str = Field(description="The reference/truth value read from the page.")
    confidence: float = Field(default=0.0, description="Heuristic confidence of the extracted value [0,1].")
    threshold: float | None = Field(
        default=None,
        description=f"Accept threshold [0,1]. If omitted, read from ${_ENV_THRESHOLD} (default {_DEFAULT_THRESHOLD}).",
    )
    accept_label: str = Field(default="Accept", description="Button label to click when accepted.")
    reject_label: str = Field(default="Reject", description="Button label to click when rejected.")
    digits_only: bool = Field(default=True, description="Compare only the digits of each value.")


class _Outputs(BaseModel):
    decision: str = Field(description="'accept' or 'reject'.")
    click_text: str = Field(description="The button label to click (accept_label / reject_label).")
    match: bool = Field(description="Whether extracted == truth (after normalization).")
    threshold_used: float = Field(description="The threshold actually applied.")
    similarity: float = Field(description="1.0 if exact match else a [0,1] char-overlap score.")


SPEC = CapabilitySpec(
    ref=CAP_REF,
    description=(
        "Decide accept vs reject for an OCR'd value against a truth value and a confidence "
        "threshold (threshold from the AAKAAR_OCR_ACCEPT_THRESHOLD env var unless overridden). "
        "Emits both the decision and the exact button label to click, so it can be wired into "
        "cap.web_click(text=...). Pure logic, read-only."
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


def _similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    same = sum(1 for i in range(n) if a[i] == b[i])
    return round(same / max(len(a), len(b)), 4)


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

    match = bool(ex) and ex == tr
    accept = match and conf >= threshold
    decision = "accept" if accept else "reject"
    out = {
        "decision": decision,
        "click_text": accept_label if accept else reject_label,
        "match": match,
        "threshold_used": round(threshold, 4),
        "similarity": _similarity(ex, tr),
    }
    logger.info("cap.value_decision extracted=%r truth=%r conf=%.3f thr=%.3f match=%s -> %s",
                ex, tr, conf, threshold, match, decision)
    return out
