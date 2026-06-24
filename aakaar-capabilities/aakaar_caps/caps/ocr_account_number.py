"""cap.ocr_account_number — OCR a cheque image with PP-OCRv5 and extract the
recipient account number, with a model confidence AND a heuristic confidence.

Reads an image from managed storage (``aakaar://`` URI), runs PP-OCRv5 (the 2025
Baidu specialist, via the ``rapidocr`` ONNX package — bundled models, fully
offline, no torch), extracts the most account-number-like digit run, and returns:

  - ``account_number``        the extracted digits
  - ``model_confidence``      PP-OCRv5's own score for that detection
  - ``heuristic_confidence``  a blend of per-digit consensus (across pre-processing
                              variants), structural validity (length / all-digits),
                              model score and digit stability — NOT model conf alone

The heavy deps (``rapidocr``/``numpy``/``PIL``) are imported lazily so the server
can register this capability without them; the host that EXECUTES it (the agent)
must have ``rapidocr`` installed. Read-only (``side_effecting=False``).
"""

from __future__ import annotations

import io
import logging
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.ocr_account_number"

_ENGINE: Any = None  # cached PP-OCRv5 engine (per process)

# Digit-confusion map for letter->digit fixes in a numeric field.
_CONF = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "|": "1", "Z": "2",
         "S": "5", "B": "8", "G": "6", "T": "7", "A": "4", "g": "9", "q": "9"}


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_uri: str = Field(description="Managed-storage URI (aakaar://...) of the cheque image to OCR.")
    expected_length: int = Field(default=14, ge=6, le=24,
        description="Expected account-number length (structural prior; HDFC=14).")
    min_length: int = Field(default=10, ge=4, le=24, description="Minimum digit-run length to consider.")
    max_length: int = Field(default=18, ge=6, le=30, description="Maximum digit-run length to consider.")


class _Outputs(BaseModel):
    account_number: str = Field(description="Best extracted account number (digits), or '' if none.")
    model_confidence: float = Field(description="PP-OCRv5's own score for the chosen detection [0,1].")
    heuristic_confidence: float = Field(description="Blended confidence (consensus+structure+model+stability) [0,1].")
    raw_text: str = Field(description="All text PP-OCRv5 read (joined), for audit.")
    candidate_count: int = Field(description="How many account-length candidates were found.")


SPEC = CapabilitySpec(
    ref=CAP_REF,
    description=(
        "OCR a cheque image (from managed storage) with PP-OCRv5 and extract the recipient "
        "account number, returning the extracted digits, the model's own confidence, and a "
        "heuristic confidence (per-digit consensus across pre-processing variants + structural "
        "validity + model score + digit stability — not model confidence alone). Offline, CPU."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("ocr", "cheque"),
    side_effecting=False,
)


def _engine() -> Any:
    global _ENGINE
    if _ENGINE is None:
        from rapidocr import RapidOCR  # type: ignore[import-not-found]  # lazy PP-OCRv5 ONNX (agent only)
        _ENGINE = RapidOCR()
    return _ENGINE


def _normalize_digits(text: str) -> tuple[str, int]:
    fixes, out = 0, []
    for ch in str(text).upper():
        if ch.isdigit():
            out.append(ch)
        elif ch in _CONF:
            out.append(_CONF[ch])
            fixes += 1
        else:
            out.append(ch)
    return "".join(out), fixes


def _ocr_variants(arr: Any) -> list[Any]:
    """A couple of cheap pre-processing variants so we get several independent
    reads of the same digits to vote on (consensus is the key confidence signal)."""
    import numpy as np
    variants = [("orig", arr)]
    gray = arr if arr.ndim == 2 else (0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2])
    gray = gray.astype("uint8")
    # Otsu threshold (pure numpy) — drops faint background, sharpens strokes.
    hist, _ = np.histogram(gray, bins=256, range=(0, 256))
    tot = gray.size
    sumv = float((np.arange(256) * hist).sum())
    sb = wb = 0.0
    best_t, best_var = 127, -1.0
    for t in range(256):
        wb += hist[t]
        if wb == 0:
            continue
        wf = tot - wb
        if wf == 0:
            break
        sb += t * hist[t]
        mb = sb / wb
        mf = (sumv - sb) / wf
        v = wb * wf * (mb - mf) ** 2
        if v > best_var:
            best_var, best_t = v, t
    otsu = ((gray > best_t).astype("uint8") * 255)
    variants.append(("otsu", np.stack([otsu] * 3, axis=-1)))
    return variants


def _run_ppocrv5(eng: Any, arr: Any) -> list[tuple[str, float]]:
    out = eng(arr)
    txts = getattr(out, "txts", None)
    res: list[tuple[str, float]] = []
    if txts is not None:
        scores = getattr(out, "scores", None) or []
        for i, t in enumerate(txts or []):
            res.append((str(t), float(scores[i]) if i < len(scores) else 0.0))
    elif out:
        seq = out[0] if isinstance(out, tuple) else out
        for item in (seq or []):
            res.append((str(item[1]), float(item[2])))
    return res


def _digit_runs(text: str, lo: int, hi: int) -> list[str]:
    norm, _ = _normalize_digits(text)
    return re.findall(r"(?<!\d)\d{%d,%d}(?!\d)" % (lo, hi), norm)


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    import numpy as np
    from PIL import Image

    image_uri = inputs["image_uri"]
    target = int(inputs.get("expected_length", 14))
    lo = int(inputs.get("min_length", 10))
    hi = int(inputs.get("max_length", 18))

    data = await ctx.read_object(image_uri)
    img = Image.open(io.BytesIO(data)).convert("RGB")
    arr = np.array(img)
    logger.info("cap.ocr_account_number start run_id=%s uri=%s size=%s", ctx.run_id, image_uri, img.size)

    eng = _engine()
    all_text: list[str] = []
    # candidate digit-string -> {best_model_conf, n_variants, fixes}
    cands: dict[str, dict[str, Any]] = {}
    for _name, variant in _ocr_variants(arr):
        for text, score in _run_ppocrv5(eng, variant):
            all_text.append(text)
            for run_digits in _digit_runs(text, lo, hi):
                _, fixes = _normalize_digits(text)
                c = cands.setdefault(run_digits, {"model": 0.0, "variants": 0, "fixes": fixes})
                c["model"] = max(c["model"], score)
                c["variants"] += 1
                c["fixes"] = min(c["fixes"], fixes)

    if not cands:
        logger.info("cap.ocr_account_number: no account-length candidate found")
        return {"account_number": "", "model_confidence": 0.0, "heuristic_confidence": 0.0,
                "raw_text": " ".join(all_text)[:2000], "candidate_count": 0}

    # Per-digit consensus pool over target-length candidates (truth-free signal).
    pool = [d for d in cands for _ in range(cands[d]["variants"]) if len(d) == target]

    def digit_agreement(d: str) -> float:
        if len(d) != target or not pool:
            return 0.0
        return sum(sum(1 for s in pool if s[i] == ch) / len(pool) for i, ch in enumerate(d)) / target

    def structural(d: str) -> float:
        n = len(d)
        if n == target:
            return 1.0
        if abs(n - target) == 1:
            return 0.6
        return 0.2 if lo <= n <= hi else 0.0

    def cand_score(d: str) -> tuple[float, float, float]:
        c = cands[d]
        agree = digit_agreement(d)
        struct = structural(d)
        stability = max(0.0, 1 - 0.1 * c["fixes"])
        heur = float(np.clip(0.35 * agree + 0.30 * struct + 0.20 * c["model"] + 0.15 * stability, 0, 1))
        return heur, struct, agree

    best = max(cands, key=lambda d: (structural(d), cand_score(d)[0], cands[d]["model"]))
    heur, _, _ = cand_score(best)
    result = {
        "account_number": best,
        "model_confidence": round(float(cands[best]["model"]), 4),
        "heuristic_confidence": round(heur, 4),
        "raw_text": " ".join(all_text)[:2000],
        "candidate_count": len(cands),
    }
    logger.info("cap.ocr_account_number ok run_id=%s acct=%s model=%.3f heur=%.3f cands=%d",
                ctx.run_id, best, result["model_confidence"], result["heuristic_confidence"], len(cands))
    return result
