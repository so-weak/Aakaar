"""Cheque OCR — extract beneficiary / cheque-no / amount / account-no
from PNG bytes captured by the CTS UAT 'F' (front) and 'B' (back)
viewers.

Why this is its own module (not inside the capability):

The actual orchestration lives in `cap.cts_uat_read_cheques` (drive the
browser, click F/B for every row, screenshot, save the bytes to the
object store). That capability calls `extract_fields(png_bytes, side=
'front'|'back')` which returns a typed result dict. Keeping OCR here
means:

  - the capability is easy to test with a fake OCR (just monkeypatch
    this module's `extract_fields`),
  - the OCR pipeline can be hit from other capabilities or scripts
    without dragging in the cap.cts_uat_read_cheques dependency
    surface (browser session, run context, etc.),
  - heavy native deps (opencv, tesseract) are imported LAZILY so the
    rest of the system stays importable when they're missing.

Approach:

  1. Decode PNG → grayscale numpy array via Pillow (always available).
  2. Apply mild preprocessing via OpenCV if available — adaptive
     threshold + denoise sharply improves Tesseract recall on the
     thin, low-contrast ink scans CTS produces. When OpenCV is
     missing we feed the raw grayscale image straight to Tesseract;
     accuracy drops but the pipeline still produces output.
  3. Run Tesseract via pytesseract.image_to_string with PSM=6
     ('assume a single uniform block of text') — best general-
     purpose mode for the rectangular cheque crops.
  4. Walk the raw text line-by-line with side-specific regexes:
       front → Pay (beneficiary), Cheque No, Amount
       back  → Account No
     Each regex captures a normalized version of the value, and the
     caller always gets the raw text back too so a missed field can
     be inspected visibly in the UI.

The regexes are deliberately permissive — cheques vary in layout per
bank/branch and OCR is noisy. Our job is to surface the operator's
best guess, not to validate the data; the user can verify against
the image thumbnail in the same UI panel.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

logger = logging.getLogger(__name__)


# Env-flag controlled "GOT-only" mode. When set truthy AND GOT-OCR2
# successfully produces a cheque-shaped read, every other engine
# (Apple Vision, paddle_or_easy baseline, micr_strip, paddle_focused_*,
# apple_vision_date, trocr_handwriting, doctr, paddle_focused_back_stamp)
# is skipped. The point of the flag is to A/B test "single-VLM
# pipeline vs current multi-engine pipeline" on real cheque batches
# without changing default behaviour:
#
#   * Flag UNSET → current behaviour, GOT is primary but every
#     downstream engine still runs with its own granular skip-gates.
#   * Flag SET + GOT produces text → all downstream engines skip;
#     `engine_runs` records a "skipped (got_only_mode active)" entry
#     for each so the operator UI still shows that the engine
#     existed but was intentionally suppressed.
#   * Flag SET + GOT FAILS / not loaded → falls back to the normal
#     multi-engine pipeline (best-effort degradation, never a hard
#     fail just because the env var was set).
#
# Promote to default only after a real cheque-batch comparison
# proves the simplification is safe. Until then, this is opt-in.
_GOT_ONLY_ENV = "AAKAAR_OCR_GOT_ONLY"


def _env_bool(name: str, default: bool = False) -> bool:
    """Permissive env-var boolean: accepts 1/true/yes/on (case-insensitive)
    as True; anything else (including empty / unset) returns `default`.
    Same shape as the helper in `got_ocr.py` and `handwriting_ocr.py`
    so the three OCR modules speak a consistent dialect."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _got_only_mode_active(
    got_regions: list[Any], got_text: str,
) -> bool:
    """Returns True when the operator has opted-in to GOT-only mode
    AND GOT-OCR2 produced a usable read on this cheque. The text-
    length gate (>= 50 chars) prevents an empty / malformed GOT
    generation from silently suppressing the rest of the pipeline."""
    if not _env_bool(_GOT_ONLY_ENV, default=False):
        return False
    if not got_regions:
        return False
    if len(got_text) < 50:
        return False
    return True


# Sentinel string written into the `missing_dep` slot of each
# engine_runs entry that was suppressed by GOT-only mode. Kept as
# a constant so tests and downstream UI can pattern-match against
# it without string-coupling to copy.
_GOT_ONLY_SKIP_MSG = (
    "skipped: AAKAAR_OCR_GOT_ONLY active — got_ocr2 produced a "
    "successful read; secondary engines suppressed"
)


# Track which engines we've already logged as cached-dead so the
# operator-facing INFO log fires exactly ONCE per process per
# engine instead of N times (one per cheque). The skip itself
# still happens silently for every cheque after the first;
# this is purely about log noise.
_logged_dead_engines: set[str] = set()


def _log_dead_engine_skip(engine_name: str, reason: str) -> None:
    """Log a one-time INFO when we skip an engine that's known
    to be unloadable. Idempotent — subsequent calls for the same
    engine name are silent. Cleared by `reset_dead_engine_log_for_tests`
    so test isolation isn't broken by module-global state."""
    if engine_name in _logged_dead_engines:
        return
    _logged_dead_engines.add(engine_name)
    logger.info(
        "cheque_ocr: %s engine permanently disabled this process — "
        "all subsequent cheques will skip the dispatch. Reason: %s",
        engine_name, reason,
    )


def reset_dead_engine_log_for_tests() -> None:
    """Clear the one-time-log tracker so tests can assert the
    log message fires deterministically. Production code never
    needs to call this."""
    _logged_dead_engines.clear()


def _apple_vision_date_match(text: str) -> str | None:
    """Scan an Apple Vision OCR text for a DDMMYYYY-shaped 8-digit
    run that parses as a plausible cheque date (year 2010 ..
    today+10). Returns the matched string or None.

    Used by the apple_vision_date skip gate — when Vision already
    found a plausible date in the whole-face text, the date-band
    read can only confirm what we have.

    Handles three layouts Apple Vision can produce on a cheque
    date band:
      1. CONTINUOUS — Vision reads the date as a single token
         "21062026" (rare; usually only when the cheque has a
         pre-printed unfaced date).
      2. SPACED — Vision reads "21 06 2026" or "21/06/2026"
         (typical when the date is hand-written in a single
         box). We strip non-digit separators and rescan.
      3. BOXED CELLS — Vision reads each cell as its OWN
         single-digit observation, producing 8 separate
         single-digit lines: "2\n1\n0\n6\n2\n0\n2\n6". This
         is the layout on SBI / HDFC CTS-2010 cheques. We
         concatenate up-to-12 adjacent single-digit lines and
         try every length-8 window.
    """
    if not text:
        return None
    import datetime as _dt  # noqa: PLC0415
    import re as _re  # noqa: PLC0415
    today_year = _dt.date.today().year

    def _is_plausible(candidate: str) -> bool:
        try:
            parsed = _dt.datetime.strptime(candidate, "%d%m%Y").date()
        except ValueError:
            return False
        return 2010 <= parsed.year <= today_year + 10

    # Layout 1 — continuous DDMMYYYY anywhere in the text.
    for candidate in _re.findall(r"\b\d{8}\b", text):
        if _is_plausible(candidate):
            return candidate

    # Layout 2 — date with separators (slashes, dashes, spaces).
    # Match D[D] sep M[M] sep YYYY and zero-pad day/month before
    # rebuilding the canonical DDMMYYYY string.
    sep_re = _re.compile(
        r"\b(\d{1,2})[/\-\s.](\d{1,2})[/\-\s.](\d{4})\b",
    )
    for d, m, y in sep_re.findall(text):
        candidate = f"{int(d):02d}{int(m):02d}{int(y):04d}"
        if _is_plausible(candidate):
            return candidate

    # Layout 3 — boxed cells: each digit (or 2-digit cell)
    # on its own line. Scan the text line-by-line and
    # concatenate runs of short all-digit lines (1-2 digits
    # per line). When the JOINED digit run is at least 8
    # digits long, try every length-8 window.
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    run: list[str] = []
    candidate_runs: list[str] = []
    for line in lines:
        # Accept single-digit cells (most common) AND short
        # 2-digit cells (some bank layouts split the year
        # into two cells of 2 digits each).
        if line.isdigit() and 1 <= len(line) <= 2:
            run.append(line)
        else:
            joined = "".join(run)
            if len(joined) >= 8:
                candidate_runs.append(joined)
            run = []
    joined = "".join(run)
    if len(joined) >= 8:
        candidate_runs.append(joined)

    for joined in candidate_runs:
        for start in range(len(joined) - 7):
            window = joined[start:start + 8]
            if _is_plausible(window):
                return window
    return None


@dataclass(frozen=True, slots=True)
class ChequeFields:
    """Result of `extract_fields(...)`. Every field is optional —
    a missing value means "OCR didn't find it" (NOT "the cheque
    has no such field"); the operator should fall back to looking
    at `raw_text` or the image directly. `missing_dep` is set when
    OCR is unavailable (paddleocr / easyocr extras not installed) —
    the caller surfaces it instead of pretending OCR failed for
    some other reason.

    `ocr_confidence` (0..1) and `ocr_rotation_deg` (0/90/180/270)
    are PaddleOCR's per-pass metadata: a low confidence is a strong
    hint that the field regex output below is unreliable; a non-zero
    rotation tells the UI that the upload was sideways/upside-down
    and the auto-orientation step kicked in. `oriented_image_uri`
    carries the de-rotated PNG so the operator can compare the OCR
    text to the same image the engine actually saw.
    """

    side: Literal["front", "back"]
    raw_text: str | None
    # Front-side fields:
    beneficiary: str | None = None
    cheque_no: str | None = None
    amount: str | None = None
    amount_words: str | None = None
    # Back-side fields:
    account_no: str | None = None
    # MICR-strip-derived fields (front only). Auto-populated by a
    # secondary OCR pass focused on the bottom ~18% of the cheque,
    # which is where the printed CTS layout lives. These come from
    # the bank's machine-printed MICR row, NOT from the handwritten
    # body, so they're substantially more reliable than the field
    # extractors above when the strip OCR succeeds.
    micr_text: str | None = None
    city: str | None = None
    bank: str | None = None
    branch: str | None = None
    tc: str | None = None
    # Handwriting-specialised OCR (front only). Populated when the
    # `cheque-handwriting` extra is installed and TrOCR is
    # available. `handwriting_regions` carries per-region reads
    # (name, text, confidence) so the UI can render each one with
    # its own confidence badge. `handwriting_missing_dep` is set
    # when TrOCR isn't installed — surfaced so the operator sees
    # why handwriting OCR didn't contribute. Distinct from
    # `missing_dep` (which only fires for the primary print engine).
    handwriting_regions: tuple[tuple[str, str, float], ...] = ()
    handwriting_missing_dep: str | None = None
    # Signature presence detection (front side only). Rule 6 of
    # the cheque validation spec: "drawee's signature must be
    # present on the cheque". `signature_verdict` is one of
    # "present" / "maybe" / "absent" so the validator can pick
    # PASS / WARN / FAIL without re-implementing the ink-density
    # threshold ladder. `signature_density` is the actual dark-
    # pixel fraction (0..1) for operator transparency.
    # `signature_missing_dep` is set when the detector couldn't
    # run (OpenCV missing) → validator downgrades to NOT_VERIFIED.
    signature_verdict: str | None = None
    signature_density: float = 0.0
    signature_missing_dep: str | None = None
    # Per-engine raw output. Each tuple is (engine_name,
    # raw_text, avg_confidence, region_count, missing_dep,
    # elapsed_ms). The UI renders these in a "Raw engine
    # outputs" panel so the operator can cross-check what each
    # plugin actually read off the cheque (vs. the consolidated
    # `raw_text` which merges them). `engine_runs` is the
    # source of truth for debugging "why is the validation
    # badge red?" — if all four engines agree but the DOM
    # panel says something else, the bank's data is suspect;
    # if engines disagree, it's an OCR problem.
    #
    # `elapsed_ms` (slot 5, added 2026-06) is the wall-time
    # cost of running this engine pass, captured via
    # `time.perf_counter()`. Used by the live activity panel to
    # show per-engine timing chips so operators can see WHICH
    # OCR pass is dragging a cheque's wall time. Five-element
    # legacy tuples are still accepted (some test fixtures
    # haven't been migrated yet) and treated as "untimed" by
    # the serialiser.
    engine_runs: tuple[tuple, ...] = ()
    # Vision-Language-Model verification result (front only).
    # When the local VLM (Qwen2.5-VL by default) is loadable AND
    # the caller passed `dom` into `extract_fields`, we run a
    # constrained-JSON verification pass that answers the six
    # per-spec validation questions directly off the image. The
    # validator rules consult this FIRST when confidence >= 0.7
    # and fall back to OCR when the VLM declined / disagreed.
    # Stored as a dict so the API → UI surface is symmetric with
    # other diagnostic blocks; the validator unpacks the keys
    # itself. Empty dict when no VLM pass ran (no `dom`, weights
    # not on disk, mlx/torch unavailable, kill-switch on).
    vlm_verification: dict[str, Any] = field(default_factory=dict)
    # Pipeline diagnostics — surfaced in the UI so the operator
    # knows whether 'no extracted fields' meant 'OCR failed to
    # load' vs. 'OCR ran but didn't find the patterns'.
    missing_dep: str | None = None
    error: str | None = None
    # OCR-pass metadata (PaddleOCR / EasyOCR).
    ocr_confidence: float = 0.0
    ocr_rotation_deg: int = 0
    # Per-region detail — kept compact (text + confidence only) so
    # the API payload stays small even with 30+ regions per side.
    ocr_regions: tuple[tuple[str, float], ...] = ()
    # Per-field consensus across all engines that voted on this
    # side. Computed by `cheque_consensus.build_consensus` after
    # all engine passes complete; each FieldConsensus carries the
    # winning value, a 0..1 trust_score, the individual per-engine
    # votes, and a `review_reason` (non-None when trust is low or
    # only one engine voted). The Phase 5/6 operator UI uses this
    # to render trust badges and the "Review queue" panel. Empty
    # tuple when no votes were collected (e.g. legacy test
    # fixtures that bypass `extract_fields`). Stored as
    # `tuple[Any, ...]` to keep the import out of the dataclass
    # signature — concrete type is
    # `tuple[cheque_consensus.FieldConsensus, ...]`.
    consensus: tuple[Any, ...] = ()
    # Cross-field validation findings. Computed by
    # `cheque_cross_field.run_all_cross_field_checks` AFTER
    # consensus is built and BEFORE the consensus' trust_score
    # gets downgraded for cross-field failures. Each entry is a
    # `CrossFieldFinding` carrying (rule_id, severity, summary,
    # affected_fields, detail). The validator and the operator UI
    # consume this to explain WHY a field's trust dropped (e.g.
    # "amount_words say 50000 but figures say 16388"). Empty
    # tuple when no findings fired. Stored as `tuple[Any, ...]`
    # for the same import-graph hygiene reason as `consensus`.
    cross_field_findings: tuple[Any, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "raw_text": self.raw_text,
            "beneficiary": self.beneficiary,
            "cheque_no": self.cheque_no,
            "amount": self.amount,
            "amount_words": self.amount_words,
            "account_no": self.account_no,
            "micr_text": self.micr_text,
            "city": self.city,
            "bank": self.bank,
            "branch": self.branch,
            "tc": self.tc,
            "handwriting_regions": [
                {"name": n, "text": t, "confidence": c}
                for (n, t, c) in self.handwriting_regions
            ],
            "handwriting_missing_dep": self.handwriting_missing_dep,
            "signature_verdict": self.signature_verdict,
            "signature_density": self.signature_density,
            "signature_missing_dep": self.signature_missing_dep,
            "engine_runs": [
                {
                    "engine": run[0],
                    "text": run[1],
                    "avg_confidence": run[2],
                    "region_count": run[3],
                    "missing_dep": run[4],
                    # Slot 5 is elapsed_ms (int) when present.
                    # Legacy fixtures may pass 5-tuples — surface
                    # `null` so the UI can render "—" instead of
                    # showing 0ms (which would imply "the engine
                    # ran instantly" rather than "we don't know").
                    "elapsed_ms": run[5] if len(run) >= 6 else None,
                }
                for run in self.engine_runs
            ],
            "vlm_verification": dict(self.vlm_verification),
            "missing_dep": self.missing_dep,
            "error": self.error,
            "ocr_confidence": self.ocr_confidence,
            "ocr_rotation_deg": self.ocr_rotation_deg,
            "ocr_regions": [
                {"text": t, "confidence": c} for (t, c) in self.ocr_regions
            ],
            "consensus": [c.to_dict() for c in self.consensus],
            "cross_field_findings": [
                f.to_dict() for f in self.cross_field_findings
            ],
        }


# ---------- Public API -----------------------------------------------------

# Fractional bbox (x_left, y_top, x_right, y_bottom) of the courtesy /
# figures amount box on a standard CTS cheque face — the band to the
# right of the "₹ / अदा करें" marker, roughly the right 40% of the
# width and the 28-52% vertical strip. Re-OCR'd at high resolution by
# the focused amount-box pass in `extract_fields`. Kept generous so it
# survives minor per-bank layout shifts.
_AMOUNT_BOX_BBOX: tuple[float, float, float, float] = (0.60, 0.28, 0.99, 0.52)

# Bbox for the handwritten 'Rupees ... Only' amount-in-words band
# on a standard CTS cheque face (cropped image). The band sits in
# the upper-middle row across the left ~60% of the cheque, just
# below the 'Pay <beneficiary>' line and to the left of the
# courtesy-amount box (which `_AMOUNT_BOX_BBOX` already covers).
#
# Calibration data: the AXIS BANK CTS cheque captured 26-Jun-2026
# is 322 x 700 (h x w) and the handwritten 'Two Lakh Only' line
# falls at roughly y ∈ [103, 148] = (0.32, 0.46) of height, x ∈
# [42, 462] = (0.06, 0.66) of width. We use the same band the
# operator UI crops for its eyeball check (see
# `cheque_validation._RULE_BAND_BBOX['amount_words']`) so the
# operator's visual crop and the OCR's input crop stay in sync —
# if one is right the other is too.
#
# Used by the focused amount-words re-OCR pass below (analogue of
# the existing `_AMOUNT_BOX_BBOX` / focused-figures pass) to
# recover the handwriting on cheques where the full-page OCR
# crushes the line into unrecognisable noise (e.g. 'OR  h /
# hjnoma sodnyph / SAI (M)' for 'Rupees Two Lakh Only').
#
# Bottom edge widened 0.46 -> 0.52 so a LARGE amount that wraps onto a
# second handwritten row ('Rupees Two Lakh Fifty' / 'Thousand Only') is
# captured whole — RapidOCR returns one region per row and the
# tokeniser joins them (it splits on whitespace incl. newlines), so the
# wrapped value parses as a single line. Kept in sync with the operator
# UI crop (`cheque_validation._RULE_BAND_BBOX['amount_words']`).
_AMOUNT_WORDS_BBOX: tuple[float, float, float, float] = (0.05, 0.32, 0.66, 0.52)


# Best-of-N preprocessing variants for the focused amount-words band.
# Ordered with the historically-best recipe first (320px CLAHE+unsharp);
# the sweep early-exits the moment a candidate's fuzzy parse recovers
# the system amount, so a clean cheque still pays a single OCR pass and
# only genuinely-hard handwriting pays for the extra variants. Kept to
# three so the per-cheque cost stays small (consistent with the recent
# advance-path speed work).
_AMOUNT_WORDS_VARIANTS: tuple[dict[str, Any], ...] = (
    {"target_height": 320, "enhance": True},
    {"target_height": 384, "enhance": True, "binarize": True},
    {"target_height": 240, "enhance": False},
)

# Same idea for the courtesy FIGURES box. A digit box benefits from
# binarisation (clean strokes) so the Otsu variant is promoted second.
_AMOUNT_FIGURES_VARIANTS: tuple[dict[str, Any], ...] = (
    {"target_height": 320, "enhance": True},
    {"target_height": 320, "enhance": True, "binarize": True},
    {"target_height": 240, "enhance": False},
)


def _anchor_amount_words_bbox(regions: list[Any] | None) -> tuple | None:
    """Derive an amount-words crop bbox anchored to the preprinted
    'Rupees' label detected in the full-page OCR `regions`, or None
    when no confident anchor is found (caller falls back to the static
    `_AMOUNT_WORDS_BBOX`).

    On a CTS cheque face the handwritten amount sits on the SAME row as
    the preprinted 'Rupees' label and runs to its right, so the label's
    box pins the band vertically across bank layouts that shift it.
    The returned band starts just left of the label and extends down a
    little past it to catch an amount that wraps to a second row.

    Coordinates: RapidOCR bboxes are pixel points (top-left origin); we
    normalise to fractions using the image extent inferred from the
    regions themselves (max point), which is exact for a full-page pass
    whose detection reaches the cheque edges and otherwise a safe
    slight over-estimate (the band is clamped to [0, 1])."""
    if not regions:
        return None
    # Infer image size from the farthest detected point. RapidOCR runs
    # detection over the whole image, so the max x / y across all boxes
    # is at or just inside the true width / height.
    max_x = 0.0
    max_y = 0.0
    for r in regions:
        for px, py in getattr(r, "bbox", None) or []:
            max_x = max(max_x, float(px))
            max_y = max(max_y, float(py))
    if max_x <= 0 or max_y <= 0:
        return None

    best_region = None
    best_ratio = 0.0
    for r in regions:
        text = (getattr(r, "text", "") or "").strip().lower()
        if len(text) < 3:
            continue
        # Tokens on the label row: match the preprinted 'rupees'.
        for tok in re.split(r"[^a-z]+", text):
            if len(tok) < 4:
                continue
            ratio = difflib.SequenceMatcher(
                a=tok, b="rupees", autojunk=False,
            ).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_region = r
    if best_region is None or best_ratio < 0.6:
        return None

    xs = [float(px) for px, _py in best_region.bbox]
    ys = [float(py) for _px, py in best_region.bbox]
    rleft = min(xs) / max_x
    rtop = min(ys) / max_y
    rbot = max(ys) / max_y
    # Band: from just left of the label to the static right edge (0.66,
    # before the figures box), from a hair above the label down ~0.16
    # past its bottom (room for a wrapped second handwritten row).
    x0 = max(0.0, rleft - 0.01)
    y0 = max(0.0, rtop - 0.03)
    y1 = min(1.0, rbot + 0.16)
    if y1 - y0 < 0.05:
        return None
    return (x0, y0, 0.66, y1)


def _focused_amount_figures_best(
    png_bytes: bytes, dom: dict[str, Any] | None,
) -> Any:
    """Best-of-N for the courtesy FIGURES box: sweep
    `_AMOUNT_FIGURES_VARIANTS` and return the RegionResult most likely
    to carry a real amount, early-exiting when a candidate's parsed
    figure equals the DOM amount.

    Scoring prefers a STRUCTURALLY decorated amount (currency / '=' /
    '/-' / decimal / comma — the strong 'we read the box' cue the
    caller's override logic also keys on), then any parseable amount,
    then recovered-text length. Returns None when no variant produced
    text, or the engine-unavailable RegionResult so the caller records
    the missing-dep diagnostic. Never raises here."""
    from aakaar_caps.cheque import rapid_ocr  # noqa: PLC0415
    from aakaar_caps.cheque.words_to_number import (  # noqa: PLC0415
        figures_to_decimal,
    )

    dom_amt_raw = ""
    if dom:
        dom_amt_raw = str(dom.get("Amount") or dom.get("Batch Amount") or "")
        if "/" in dom_amt_raw and not dom_amt_raw.endswith("/"):
            dom_amt_raw = dom_amt_raw.rsplit("/", 1)[-1].strip()
    dom_value = figures_to_decimal(dom_amt_raw) if dom_amt_raw else None

    best: Any = None
    best_score = -1.0
    for variant in _AMOUNT_FIGURES_VARIANTS:
        box = rapid_ocr.run_ocr_on_region(
            png_bytes, _AMOUNT_BOX_BBOX, **variant,
        )
        if box.missing_dep:
            return box
        text = (box.text or "").strip()
        if not text:
            continue
        lines = box.text.splitlines()
        any_amt = _find_amount_in_figures(lines)
        if (
            dom_value is not None
            and any_amt is not None
            and figures_to_decimal(any_amt) == dom_value
        ):
            return box
        decorated = _find_amount_in_figures(lines, decorated_only=True)
        score = (
            2.0 if decorated is not None
            else (1.0 if any_amt is not None else 0.0)
        ) + min(len(text), 50) / 1000.0
        if score > best_score:
            best_score = score
            best = box
    return best


def _focused_amount_words_best(
    png_bytes: bytes,
    dom: dict[str, Any] | None,
    regions: list[Any] | None = None,
) -> Any:
    """Run the focused amount-words band through `_AMOUNT_WORDS_VARIANTS`
    (plus a 'Rupees'-anchored crop variant when `regions` let us locate
    one) and return the RegionResult whose OCR text best matches the
    EXPECTED words (derived from the DOM/system amount), early-exiting
    as soon as a candidate's fuzzy parse equals the DOM amount.

    Scoring: when the DOM amount is known, candidates are ranked by
    expected-token coverage (how much of the expected words the crop
    recovered); otherwise we fall back to recovered-text length so we
    still prefer the most readable crop. The anchored crop is added as
    an EXTRA candidate, never a replacement — the static band is always
    tried, so anchoring can only help (a worse anchored read just loses
    the scoring). Returns None when no variant produced any text, or the
    engine-unavailable RegionResult so the caller can record the
    missing-dep diagnostic. Never raises here — the caller wraps this in
    its own try/except too.
    """
    from aakaar_caps.cheque import rapid_ocr  # noqa: PLC0415
    from aakaar_caps.cheque.words_to_number import (  # noqa: PLC0415
        decimal_to_words,
        expected_token_coverage,
        figures_to_decimal,
        words_to_decimal,
    )

    dom_amt_raw = ""
    if dom:
        dom_amt_raw = str(dom.get("Amount") or dom.get("Batch Amount") or "")
        if "/" in dom_amt_raw and not dom_amt_raw.endswith("/"):
            dom_amt_raw = dom_amt_raw.rsplit("/", 1)[-1].strip()
    dom_value = figures_to_decimal(dom_amt_raw) if dom_amt_raw else None
    expected = decimal_to_words(dom_value) if dom_value is not None else None

    # Build the candidate list: each entry is (bbox, kwargs). Static
    # band variants first (historically-best recipe leads), then the
    # anchored band (if found) with the default enhance recipe.
    candidates: list[tuple[tuple, dict]] = [
        (_AMOUNT_WORDS_BBOX, variant) for variant in _AMOUNT_WORDS_VARIANTS
    ]
    anchored = _anchor_amount_words_bbox(regions)
    if anchored is not None and anchored != _AMOUNT_WORDS_BBOX:
        candidates.append(
            (anchored, {"target_height": 320, "enhance": True}),
        )

    best: Any = None
    best_score = -1.0
    for bbox, variant in candidates:
        box = rapid_ocr.run_ocr_on_region(png_bytes, bbox, **variant)
        if box.missing_dep:
            # Engine can't run — further variants are pointless; hand the
            # missing-dep result back so the caller records it.
            return box
        text = (box.text or "").strip()
        if not text:
            continue
        if (
            dom_value is not None
            and words_to_decimal(text, fuzzy=True) == dom_value
        ):
            # This crop's fuzzy parse nails the system amount — done.
            return box
        if expected is not None:
            score = expected_token_coverage(text, expected)
            # Full coverage with no DOM-parse hit is as good as this
            # signal gets — stop sweeping.
            if score >= 1.0:
                return box
        else:
            score = float(len(text))
        if score > best_score:
            best_score = score
            best = box
    return best


def extract_fields(
    png_bytes: bytes,
    *,
    side: Literal["front", "back"],
    dom: dict[str, Any] | None = None,
) -> ChequeFields:
    """Run RapidOCR on a cheque image and pull out the side-relevant
    fields.

    OCR engine: RapidOCR PP-OCR via ``onnxruntime`` (see
    ``rapid_ocr.py``) — the single, cross-platform, CPU-only engine.
    Always returns a ChequeFields, never raises. Inspect
    ``result.missing_dep`` first ("RapidOCR couldn't run at all") then
    ``result.error`` ("OCR ran but threw"). When both are None and the
    named fields are also None, OCR succeeded but no regex matched —
    fall back to ``raw_text`` / the DOM presence check.

    Front side additionally runs the MICR strip pass (``micr.py``,
    which also rides on RapidOCR) and the signature-presence detector.
    ``dom`` is the bank's parsed-fields panel, used here only to hint
    the back-side account-number picker.

    The previous multi-engine stack (GOT-OCR2, Apple Vision,
    PaddleOCR/EasyOCR, docTR, TrOCR, the local VLM) was removed in
    favour of RapidOCR alone — faster, lighter, fully offline, and not
    macOS-locked.
    """
    from aakaar_caps.cheque import rapid_ocr  # noqa: PLC0415

    # ----- Phase 1: full-page OCR (RapidOCR PP-OCR via onnxruntime) -----
    _t0 = time.perf_counter()
    regions: list[Any] = []
    rapid_missing: str | None = rapid_ocr.missing_dep()
    if rapid_missing is None:
        try:
            regions = rapid_ocr.run_ocr_detail(png_bytes)
        except BaseException as e:  # noqa: BLE001
            logger.warning("cheque_ocr: rapidocr pass raised: %s", e)
            rapid_missing = f"rapidocr raised: {type(e).__name__}: {e}"
    elapsed_ms = int((time.perf_counter() - _t0) * 1000)

    if rapid_missing and not regions:
        # Engine unavailable AND no regions — surface the reason so the
        # operator sees "OCR couldn't run" instead of "no fields found".
        return ChequeFields(
            side=side,
            raw_text=None,
            missing_dep=rapid_missing,
            engine_runs=(
                ("rapidocr_ppocr", "", 0.0, 0, rapid_missing, elapsed_ms),
            ),
        )

    confs = [r.confidence for r in regions]
    avg = sum(confs) / len(confs) if confs else 0.0
    text = "\n".join(r.text for r in regions if r.text)
    region_tuples = tuple((r.text, r.confidence) for r in regions)
    logger.info(
        "cheque_ocr: rapidocr %s elapsed=%dms regions=%d avg_conf=%.2f",
        side, elapsed_ms, len(regions), avg,
    )

    engine_runs_list: list[tuple] = [
        ("rapidocr_ppocr", text, avg, len(regions), rapid_missing, elapsed_ms),
    ]

    if not regions:
        # OCR ran but found nothing (blank / solid-colour image). Empty
        # result + diagnostics so the UI shows "no regions" rather than
        # a generic "not detected".
        return ChequeFields(
            side=side,
            raw_text="",
            ocr_confidence=0.0,
            ocr_regions=(),
            engine_runs=tuple(engine_runs_list),
        )

    # ----- Phase 2: structured field extraction -----
    if side == "front":
        fields = _extract_front_fields(text)
    else:
        # Pass the DOM account number as a hint so the picker can
        # disambiguate among multiple long digit runs on the back.
        fields = _extract_back_fields(
            text, dom_account_hint=_dom_account_hint(dom),
        )

    # ----- MICR strip enrichment (front only) -----
    # The printed MICR row at the bottom of the cheque face carries the
    # City/Bank/Branch/TC + the printed cheque serial. ``run_micr_ocr``
    # crops the bottom strip, builds enhancement variants, and OCRs them
    # with RapidOCR. Total-function — never raises.
    micr_text: str | None = None
    micr_parsed: dict[str, str] = {}
    if side == "front":
        _micr_t0 = time.perf_counter()
        try:
            from aakaar_caps.cheque import micr  # noqa: PLC0415
            micr_result = micr.run_micr_ocr(png_bytes)
            if micr_result.text:
                micr_text = micr_result.text
                micr_parsed = micr_result.parsed
                # Append to raw_text so `validate_dom_presence` finds the
                # strip digits without any extra plumbing.
                text = (text + "\n" + micr_text).strip()
                logger.info(
                    "cheque_ocr: MICR strip enrichment parsed=%s",
                    sorted(micr_parsed.keys()),
                )
            micr_confs = (
                [c for _t, c in micr_result.regions]
                if micr_result.regions else []
            )
            engine_runs_list.append((
                "micr_strip",
                micr_result.text or "",
                sum(micr_confs) / len(micr_confs) if micr_confs else 0.0,
                len(micr_result.regions),
                None,
                int((time.perf_counter() - _micr_t0) * 1000),
            ))
        except Exception as e:  # noqa: BLE001
            logger.warning("cheque_ocr: MICR strip pass failed (%s)", e)
            engine_runs_list.append((
                "micr_strip", "", 0.0, 0, f"micr pass failed: {e}",
                int((time.perf_counter() - _micr_t0) * 1000),
            ))

    # The MICR-parsed cheque_no (printed E-13B digits) beats the body-
    # text extractor's guess; prefer it when present.
    cheque_no = micr_parsed.get("cheque_no") or fields.cheque_no

    # ----- Focused amount-box pass (front only) -----
    # The handwritten courtesy-amount box (right of the ₹ glyph) is a
    # tiny ~30px band that the full-page RapidOCR pass routinely mangles
    # (e.g. '47605=00' read as '00=509Lh'). Re-OCR just that box at high
    # resolution with contrast enhancement. We only OVERRIDE the
    # full-page amount when the focused crop yields a STRUCTURALLY
    # decorated amount (currency marker / '=' or '/-' paise terminator /
    # decimal / comma) — a strong, DOM-independent cue that we read the
    # real box — or when the full page found no amount at all.
    amount_figures = fields.amount
    if side == "front":
        try:
            # Best-of-N: sweep preprocessing variants and keep the crop
            # most likely to carry a real figure (early-exit on a DOM
            # match). None when no variant produced text.
            box = _focused_amount_figures_best(png_bytes, dom)
            if box is not None and box.text:
                box_lines = box.text.splitlines()
                decorated = _find_amount_in_figures(
                    box_lines, decorated_only=True,
                )
                if decorated is not None:
                    amount_figures = decorated
                    engine_runs_list.append((
                        "rapidocr_focused_amount", box.text, box.confidence,
                        box.region_count, None, 0,
                    ))
                elif amount_figures is None:
                    any_amt = _find_amount_in_figures(box_lines)
                    if any_amt is not None:
                        amount_figures = any_amt
                        engine_runs_list.append((
                            "rapidocr_focused_amount", box.text,
                            box.confidence, box.region_count, None, 0,
                        ))
        except Exception as e:  # noqa: BLE001
            logger.warning("cheque_ocr: focused amount pass failed (%s)", e)

    # ----- Focused amount-WORDS pass (front only) -----
    # The handwritten 'Rupees ... Only' line on a cheque face is
    # cursive on a low-contrast pre-printed band. Full-page RapidOCR
    # — even at 98% average confidence on the page overall — routinely
    # crushes this single band into unrecognisable garbage (operator-
    # observed on the 26-Jun-2026 AXIS BANK fixture: 'Rupees Two Lakh
    # Only' came out as 'OR  h / hjnoma sodnyph / SAI (M)' — no
    # 'Rupees' anchor, no 'Only' anchor, so `_find_amount_in_words`
    # returns None and the amount-in-words rule lands at NOT_VERIFIED
    # with nothing to show the operator). Re-OCR JUST that band at
    # ~10x the source resolution with CLAHE + unsharp enhancement
    # (the same recipe as the focused-figures pass) and the same
    # cheque yields 'Ropeos Iwo lulch Ouly' — a clearly-readable
    # 'Rupees Two Lakh Only' that an operator can confirm in one
    # glance against the DOM-derived expected words.
    #
    # We use the focused-pass text whenever the full-page extractor
    # returned nothing OR when the focused result is meaningfully
    # longer (the focused crop almost always wins on this band; the
    # length check guards against rare degenerate cases where the
    # crop landed on a blank region and produced a 2-character
    # noise read).
    #
    # The focused crop is geometrically defined to be the amount-
    # words band by construction, so there's no mis-target risk —
    # the rule layer is told to trust this engine's output (see
    # the corresponding skip-mis-target-guard branch in
    # `_rule_amount_in_words`).
    if side == "front":
        try:
            _focused_words_t0 = time.perf_counter()
            # Best-of-N: sweep a few preprocessing variants (plus a
            # 'Rupees'-anchored crop derived from the full-page regions)
            # and keep the crop whose text best matches the expected
            # (DOM-derived) words, early-exiting on a fuzzy-parse DOM
            # hit. None when no variant produced text (a blank/illegible
            # band) — skip cleanly, no engine_run recorded.
            words_box = _focused_amount_words_best(png_bytes, dom, regions)
            if words_box is not None:
                # Sanitise: drop trailing/leading lines that are PURE
                # 1-3 digit runs. The amount-words band sits adjacent to
                # the courtesy-amount box, and the crop frequently picks
                # up a stray digit from that neighbour ('Ropeos Iwo
                # lulch Ouly' comes out as 'Ropeos Iwo lulch Ouly\n31'
                # because '31' is the leading edge of '200000' bleeding
                # in). The stray digit fools the permissive amount-words
                # parser into emitting an absurd value (31 against a DOM
                # of 200000) that masquerades as a real mismatch.
                cleaned_lines = [
                    ln for ln in (words_box.text or "").splitlines()
                    if not re.fullmatch(r"\s*\d{1,3}\s*", ln)
                ]
                focused_words_text = "\n".join(cleaned_lines).strip()
                existing = (fields.amount_words or "").strip()
                if focused_words_text and (
                    not existing or len(focused_words_text) > len(existing) + 4
                ):
                    fields = replace(fields, amount_words=focused_words_text)
                if focused_words_text or words_box.missing_dep:
                    engine_runs_list.append((
                        "rapidocr_focused_amount_words",
                        words_box.text or "",
                        words_box.confidence,
                        words_box.region_count,
                        words_box.missing_dep,
                        int((time.perf_counter() - _focused_words_t0) * 1000),
                    ))
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "cheque_ocr: focused amount-words pass failed (%s)", e,
            )

    # ----- Signature presence detection (front only, Rule 6) -----
    signature_verdict: str | None = None
    signature_density: float = 0.0
    signature_missing_dep: str | None = None
    if side == "front":
        try:
            from aakaar_caps.cheque import signature_detector  # noqa: PLC0415
            sig = signature_detector.detect_signature(png_bytes)
            signature_verdict = sig.verdict if not sig.missing_dep else None
            signature_density = sig.density
            signature_missing_dep = sig.missing_dep
        except Exception as e:  # noqa: BLE001
            logger.warning("cheque_ocr: signature detector failed (%s)", e)
            signature_missing_dep = f"signature detector failed: {e}"

    # ----- Phase 3: per-field consensus across engine_runs -----
    # With a single engine, consensus mostly carries one vote per field
    # (RapidOCR + the MICR strip's cheque_no), but the structure is kept
    # so the operator UI's trust badges / review queue keep working and
    # the cross-field checks below have something to consume. Failures
    # here must NOT break the pipeline.
    consensus_tuple: tuple = ()
    cross_field_findings: tuple = ()
    try:
        from aakaar_caps.cheque import cheque_consensus  # noqa: PLC0415
        _votes = _collect_consensus_votes(
            side=side,
            engine_runs=engine_runs_list,
            cheque_no_from_micr=cheque_no,
            dom_account_hint=(
                (dom or {}).get("account_no") if side == "back" else None
            ),
        )
        consensus_tuple = cheque_consensus.build_consensus(_votes)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "cheque_ocr: consensus build failed (%s) — continuing "
            "without consensus block; field extraction unaffected.", e,
        )

    # ----- Phase 4: cross-field validation -----
    if consensus_tuple:
        try:
            from aakaar_caps.cheque import cheque_cross_field  # noqa: PLC0415
            cross_field_findings = (
                cheque_cross_field.run_all_cross_field_checks(consensus_tuple)
            )
            if cross_field_findings:
                consensus_tuple = (
                    cheque_cross_field.apply_findings_to_consensus(
                        consensus_tuple, cross_field_findings,
                    )
                )
                logger.info(
                    "cheque_ocr: cross-field findings on side=%s: %s",
                    side,
                    [f"{f.rule_id}:{f.severity}" for f in cross_field_findings],
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "cheque_ocr: cross-field validation failed (%s) — "
                "continuing without cross-field downgrade.", e,
            )

    return ChequeFields(
        side=side,
        raw_text=text,
        beneficiary=fields.beneficiary,
        cheque_no=cheque_no,
        amount=amount_figures,
        amount_words=fields.amount_words,
        account_no=fields.account_no,
        micr_text=micr_text,
        city=micr_parsed.get("city"),
        bank=micr_parsed.get("bank"),
        branch=micr_parsed.get("branch"),
        tc=micr_parsed.get("tc"),
        signature_verdict=signature_verdict,
        signature_density=signature_density,
        signature_missing_dep=signature_missing_dep,
        engine_runs=tuple(engine_runs_list),
        ocr_confidence=avg,
        ocr_rotation_deg=0,
        ocr_regions=region_tuples,
        consensus=consensus_tuple,
        cross_field_findings=cross_field_findings,
    )


async def extract_fields_async(
    png_bytes: bytes,
    *,
    side: Literal["front", "back"],
    dom: dict[str, Any] | None = None,
) -> ChequeFields:
    """Async wrapper for `extract_fields`. Dispatches the full
    OCR pipeline to a worker thread so the caller's event loop
    stays unblocked.

    Why this exists: the cheque capability needs to overlap the
    front-side OCR pipeline (~5-10 s of native engine work) with
    the back-side flip + capture (~1-2 s of browser-driven
    network + a smart-settle poll). Running both as
    `asyncio.create_task` over `extract_fields_async` lets the
    event loop schedule the back capture while a thread chews on
    front OCR. The native OCR engines (Paddle, docTR, TrOCR,
    VLM) all release the GIL during their actual inference calls
    so the threads run truly concurrently with the asyncio I/O.

    All engine native runtimes hold their own process-wide
    singletons + locks (see `paddle_ocr._get_paddle_reader`,
    `doctr_ocr._get_doctr_predictor`, etc.) so dispatching from
    a thread is safe — concurrent calls from different threads
    serialise on the engine's own lock without dropping data.

    Internal engine concurrency (docTR + Paddle in parallel,
    VLM concurrent with focused passes) is a known future
    optimization, see the NOTE inside `extract_fields`.
    """
    return await asyncio.to_thread(
        extract_fields, png_bytes, side=side, dom=dom,
    )


# ---------- VLM-prompt helpers --------------------------------------------
#
# These pull the system-of-record values out of the bank's parsed
# panel and shape them into the simple Python types the VLM
# verifier wants. Kept here (and not in vlm_verifier.py) so the
# DOM-shape knowledge stays in one place — the validator and the
# UI also reach into the same DOM dict via the same key
# variants.


def _vlm_candidate_payees(dom: dict[str, Any]) -> list[str]:
    """Build the candidate-payees list for the VLM prompt.

    A cheque viewer's payee field is canonically "Beneficiary" on
    HDFC CTS UAT but the same panel surfaces "Beneficiary Name" /
    "Payee" / "Drawer" on adjacent screens. Include every plausible
    label, deduplicated and trimmed — the VLM then picks the one
    that matches the cheque image (or 'neither' / 'unreadable').
    """
    keys = (
        "Beneficiary", "Beneficiary Name", "Payee", "Pay to",
        "Drawer", "beneficiary", "payee",
    )
    out: list[str] = []
    seen: set[str] = set()
    for k in keys:
        v = dom.get(k)
        if not v:
            continue
        s = str(v).strip()
        if not s:
            continue
        norm = s.upper()
        if norm in seen:
            continue
        seen.add(norm)
        out.append(s)
    return out


def _vlm_dom_date(dom: dict[str, Any]) -> str | None:
    """Pull a cheque-date hint out of the DOM panel. The CTS viewer
    rarely surfaces the date in the parsed panel (it's read
    primarily off the cheque face), but when present it's a useful
    anchor for the VLM. Returns 8-digit DDMMYYYY or None."""
    raw = dom.get("Date") or dom.get("Cheque Date") or dom.get("date")
    if not raw:
        return None
    digits = re.sub(r"[^\d]", "", str(raw))
    if len(digits) != 8:
        return None
    return digits


def _vlm_avg_confidence(result: Any) -> float:
    """Average the VLM's per-answer confidences for the engine_runs
    avg_confidence cell. Zero answers (missing_dep set) → 0.0."""
    confs: list[float] = []
    for attr in (
        "payee_confidence",
        "amount_in_figures_confidence",
        "amount_in_words_confidence",
        "cheque_no_confidence",
        "date_confidence",
        "account_no_confidence",
        "signature_confidence",
    ):
        try:
            v = float(getattr(result, attr, 0.0) or 0.0)
        except (TypeError, ValueError):
            v = 0.0
        if v > 0:
            confs.append(v)
    return sum(confs) / len(confs) if confs else 0.0


def _vlm_answer_count(result: Any) -> int:
    """Count of VLM answers that came back non-'unreadable'.
    Mirrors the region_count convention of the other engine_runs
    entries (a 0 here means 'VLM ran but read nothing'; a 7 means
    'VLM gave a definitive answer on every field')."""
    n = 0
    for attr in (
        "payee_match", "amount_in_figures_matches",
        "amount_in_words_matches", "cheque_no_matches",
        "date_ddmmyyyy", "account_no_matches", "signature_present",
    ):
        if getattr(result, attr, None) is not None:
            n += 1
    return n


# ---------- Field extraction ----------------------------------------------
#
# Regex strategy:
#
# Cheque layouts vary, so we DON'T try to locate fields by absolute
# position. We work on the OCR text linearly:
#
#   - Beneficiary lives on the line that starts with PAY / PAY TO
#     (sometimes "Pay to the order of"). We take everything to the
#     right of "Pay" up to the next currency-shaped token.
#   - Cheque No is a 6+ digit run, usually printed top-right but
#     repeated in the MICR line at the bottom. The MICR run is the
#     more reliable source because it's printed in a standard E13B
#     font Tesseract handles well; we prefer the bottom-most match.
#   - Amount in figures is `Rs. NNN,NNN.NN` or `INR NNN.NN` or just
#     a digit-comma-dot number near a `₹` glyph. We extract the
#     whole numeric token and let the UI display it verbatim.
#   - Amount in words is the long line under "Rupees"; useful as a
#     human-readable cross-check.
#   - Account No (back side) is a 9+ digit run, often labelled
#     "A/C", "Acct", "Account". We accept either pattern and take
#     the longest match.

_CURRENCY_TOKEN = re.compile(
    r"(?:(?:Rs?\.?|INR|₹)\s*)?[0-9]{1,3}(?:[,\s][0-9]{2,3})*(?:\.[0-9]{1,2})?",
)


def _replace_field(base: ChequeFields, **overrides: Any) -> ChequeFields:
    """Return a copy of `base` with `overrides` applied. Convenience
    around `dataclasses.replace` so the overriding code below stays
    readable; the dataclass is frozen so we can't mutate in place."""
    from dataclasses import replace  # noqa: PLC0415
    return replace(base, **overrides)


def _dom_account_hint(dom: dict[str, Any] | None) -> str | None:
    """Pull the bank's authoritative account number out of the DOM
    panel dict (under any of its known alias keys). Returned as
    the raw string so the caller can preserve formatting; the
    picker normalises to digits-only internally. None when no
    account-number key is present."""
    if not dom:
        return None
    for key in ("Account No", "Account No.", "A/C No", "A/C No.", "A/c No"):
        value = dom.get(key)
        if value:
            return str(value)
    return None


def _back_account_already_matched(
    dom: dict[str, Any] | None,
    current_account_no: str | None,
) -> bool:
    """Return True when the paddle baseline's back-side account_no
    is already a good-enough match for the DOM hint that we can
    confidently skip the additional rescue passes (docTR + focused
    stamp). A match here means:

      * The DOM panel exposes an account number for this cheque, AND
      * paddle's `current_account_no` digits exactly equal the DOM
        digits, OR one is a suffix/prefix of the other (the bank
        sometimes truncates the leading branch code, or paddle
        misses the leading 0).

    Returns False whenever there's no DOM hint at all — without
    an authoritative target we can't tell whether paddle was
    right, so the rescue passes MUST run."""
    dom_hint = _dom_account_hint(dom)
    dom_hint_digits = _digits_only(dom_hint or "")
    current_digits = _digits_only(current_account_no or "")
    if not (dom_hint_digits and current_digits):
        return False
    return (
        current_digits == dom_hint_digits
        or current_digits.endswith(dom_hint_digits)
        or dom_hint_digits.endswith(current_digits)
    )


def _extract_front_fields(raw_text: str) -> ChequeFields:
    lines = _clean_lines(raw_text)

    beneficiary = _find_beneficiary(lines)
    cheque_no = _find_cheque_no(lines)
    amount = _find_amount_in_figures(lines)
    amount_words = _find_amount_in_words(lines)

    return ChequeFields(
        side="front",
        raw_text=raw_text,
        beneficiary=beneficiary,
        cheque_no=cheque_no,
        amount=amount,
        amount_words=amount_words,
    )


# ---------------------------------------------------------------------------
# Consensus vote collection (Phase 2)
# ---------------------------------------------------------------------------
#
# Per-engine field votes for `cheque_consensus.build_consensus`.
# The composite-text extractor (`_extract_front_fields`) gives ONE
# value per field from the union of all engines' text. For
# consensus we need each engine's OWN reading of each field, so we
# re-run the side's extractor on each full-page engine's text
# independently. Per-band engines (paddle_focused_*, apple_vision_date,
# micr_strip) contribute direct votes on their respective bands.
#
# Engine confidence in a vote = the engine_run's avg_confidence.
# We clamp to [0.05, 0.99] so a 0.0-conf engine still casts a
# weak vote rather than being dropped (sometimes the only engine
# that produced a value has avg_conf=0 because confidence wasn't
# computed for that pass) and so a 1.0-conf engine doesn't pin
# the whole consensus.

# Full-page engines whose raw text should be passed through the
# field extractors for per-engine votes. Listed in priority order
# for documentation only; voting weights come from each engine's
# own confidence.
_FULL_PAGE_ENGINES_FRONT: tuple[str, ...] = (
    "rapidocr_ppocr",
)

_FULL_PAGE_ENGINES_BACK: tuple[str, ...] = (
    "rapidocr_ppocr",
)

# Map paddle_focused_<band> engine name → ChequeFields field name
# the band's text votes on.
_FOCUSED_BAND_TO_FIELD: dict[str, str] = {
    "paddle_focused_payee_line":     "beneficiary",
    "paddle_focused_amount_words":   "amount_words",
    "paddle_focused_amount_figures": "amount",
    "paddle_focused_date":           "date",
}


def _clamp_vote_conf(conf: float) -> float:
    """Map an engine_run's raw avg_confidence to the [0.05, 0.99]
    range used for consensus weighting. See module docstring."""
    if not isinstance(conf, (int, float)) or conf != conf:  # noqa: PLR0124
        return 0.05  # NaN guard
    if conf <= 0.0:
        return 0.05
    if conf >= 1.0:
        return 0.99
    return float(conf)


def _collect_consensus_votes(
    side: Literal["front", "back"],
    engine_runs: Sequence[tuple],
    cheque_no_from_micr: str | None = None,
    dom_account_hint: str | None = None,
) -> dict[str, list[Any]]:
    """Build the per-field vote dict for `cheque_consensus.build_consensus`.

    Returns `{field_name: [FieldVote, ...]}` shaped exactly as the
    consensus builder expects. Imports `cheque_consensus` lazily
    so this module's existing import graph isn't perturbed (and so
    test fixtures that monkey-patch `cheque_consensus` keep
    working).
    """
    from aakaar_caps.cheque import cheque_consensus  # noqa: PLC0415

    votes: dict[str, list[Any]] = {}

    def _add(field_name: str, vote: Any) -> None:
        votes.setdefault(field_name, []).append(vote)

    full_page_engines = (
        _FULL_PAGE_ENGINES_FRONT if side == "front" else _FULL_PAGE_ENGINES_BACK
    )

    for run in engine_runs:
        if not run or len(run) < 3:
            continue
        engine_name = run[0]
        engine_text = (run[1] or "")
        engine_conf = _clamp_vote_conf(run[2])
        engine_text_stripped = engine_text.strip()

        if engine_name in full_page_engines and engine_text_stripped:
            # Run the side's extractor on JUST this engine's text.
            if side == "front":
                eng_fields = _extract_front_fields(engine_text)
                if eng_fields.beneficiary:
                    _add("beneficiary", cheque_consensus.make_vote(
                        engine_name, "beneficiary",
                        eng_fields.beneficiary, engine_conf,
                        cheque_consensus.default_bbox_for_field("beneficiary"),
                    ))
                if eng_fields.amount:
                    _add("amount", cheque_consensus.make_vote(
                        engine_name, "amount",
                        eng_fields.amount, engine_conf,
                        cheque_consensus.default_bbox_for_field("amount"),
                    ))
                if eng_fields.amount_words:
                    _add("amount_words", cheque_consensus.make_vote(
                        engine_name, "amount_words",
                        eng_fields.amount_words, engine_conf,
                        cheque_consensus.default_bbox_for_field("amount_words"),
                    ))
                if eng_fields.cheque_no:
                    _add("cheque_no", cheque_consensus.make_vote(
                        engine_name, "cheque_no",
                        eng_fields.cheque_no, engine_conf,
                        cheque_consensus.default_bbox_for_field("cheque_no"),
                    ))
                # Date extraction off the front text — use the
                # consensus normalizer which accepts DDMMYYYY /
                # DD-MM-YYYY / DD/MM/YYYY / DD.MM.YYYY shapes.
                date_norm = cheque_consensus.normalize_date(engine_text)
                if date_norm:
                    _add("date", cheque_consensus.make_vote(
                        engine_name, "date",
                        date_norm, engine_conf,
                        cheque_consensus.default_bbox_for_field("date"),
                    ))
            else:  # back
                eng_fields = _extract_back_fields(
                    engine_text, dom_account_hint=dom_account_hint,
                )
                if eng_fields.account_no:
                    _add("account_no", cheque_consensus.make_vote(
                        engine_name, "account_no",
                        eng_fields.account_no, engine_conf,
                        cheque_consensus.default_bbox_for_field("account_no"),
                    ))
            continue

        # Per-band engines: emit ONE direct vote on the band's field.
        focused_field = _FOCUSED_BAND_TO_FIELD.get(engine_name)
        if focused_field and engine_text_stripped:
            _add(focused_field, cheque_consensus.make_vote(
                engine_name, focused_field,
                engine_text_stripped, engine_conf,
                cheque_consensus.default_bbox_for_field(focused_field),
            ))
            continue

        if engine_name == "apple_vision_date" and engine_text_stripped:
            _add("date", cheque_consensus.make_vote(
                engine_name, "date",
                engine_text_stripped, engine_conf,
                cheque_consensus.default_bbox_for_field("date"),
            ))
            continue

        if engine_name == "micr_strip" and engine_text_stripped:
            # MICR strip carries the printed cheque_no in its first
            # group of 6 digits when CTS-2010 compliant. The MICR
            # parser already cracked it out into `cheque_no_from_micr`
            # which we emit here as a SEPARATE vote so consensus sees
            # the MICR engine's reading distinctly from any printed-
            # face engine reads. We also vote on the MICR strip's
            # full text as a low-weight backup signal for the
            # account-number cross-check (Phase 3 will use this).
            if cheque_no_from_micr:
                _add("cheque_no", cheque_consensus.make_vote(
                    "micr_strip", "cheque_no",
                    cheque_no_from_micr, engine_conf,
                    cheque_consensus.default_bbox_for_field("cheque_no"),
                ))
            continue

        if engine_name == "trocr_handwriting":
            # TrOCR exposes per-region reads via handwriting_regions
            # on ChequeFields; the engine_run.text is the
            # concatenated form. We can't re-derive per-band votes
            # here without the regions, so we vote on the four
            # bands by re-running the consensus normalizers on each
            # line of the concat. Conservative fallback: only emit
            # if the concat parses cleanly.
            #
            # Practical note: in production, TrOCR per-region reads
            # are already injected as `paddle_focused_*` overrides
            # by the existing pipeline, so the bands are covered.
            # We leave TrOCR as a passthrough voter for now and
            # revisit once TrOCR weights are actually downloadable
            # on operator machines.
            continue

    return votes


def _extract_back_fields(
    raw_text: str,
    *,
    dom_account_hint: str | None = None,
) -> ChequeFields:
    """Extract back-side structured fields from OCR text.

    `dom_account_hint`, when supplied, is the bank's authoritative
    account number for this cheque (from the CTS UAT panel). The
    extractor uses it ONLY to break ties among candidate digit
    runs — a candidate that matches (exact / suffix / prefix /
    fuzzy substring) the hint wins over a longer-but-irrelevant
    run. This is the fix for the production case where the back
    OCR contained both the correct stamped account number AND a
    longer transaction-reference line; the longest-wins picker
    grabbed the reference and reported it as the account, even
    though the DOM hint would have disambiguated cleanly.
    """
    lines = _clean_lines(raw_text)
    account_no = _find_account_no(lines, dom_account_hint=dom_account_hint)
    return ChequeFields(
        side="back",
        raw_text=raw_text,
        account_no=account_no,
    )


def _clean_lines(text: str) -> list[str]:
    """Strip OCR noise per line, drop blanks. We keep punctuation
    because the cheque-no / amount regexes need it; we only collapse
    runs of whitespace into single spaces (Tesseract sometimes
    spreads a single glyph across several space-separated columns
    when it's uncertain about column boundaries)."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        # Drop lines that are basically all OCR junk (single
        # character, all symbols) — they only contribute false
        # positives to the field finders.
        if len(line) < 2:
            continue
        line = re.sub(r"\s+", " ", line)
        out.append(line)
    return out


def _find_beneficiary(lines: list[str]) -> str | None:
    """Look for a 'PAY' line and return the beneficiary name. Two
    layouts handled (in priority order):

      1. INLINE: 'PAY <NAME>' all on one line (Paddle/Easy/Tesseract
         layout — they tend to glue adjacent regions together).
         Take everything to the right of 'PAY' up to (but excluding)
         the amount token.

      2. LINE-PER-REGION: 'PAY' on its own line, the name on the
         next line(s) (Apple Vision layout — each text observation
         is its own line). Walk forward 1-3 lines, skipping
         template boilerplate ('RUPEES', 'ONLY', 'VALID', etc.),
         and return the first plausible name line.

    Falls back to None if neither layout matches."""
    pay_re = re.compile(r"\bPAY(?:\s*TO(?:\s*THE\s*ORDER\s*OF)?)?\b[:\s\-]*", re.IGNORECASE)
    for idx, line in enumerate(lines):
        m = pay_re.search(line)
        if not m:
            continue
        tail = line[m.end():].strip()
        if tail:
            # Layout 1: inline. Cut the amount token off the end
            # of the line if present (some banks print
            # "Pay <name> ........ ₹10,000.00" all on one line).
            amt = _CURRENCY_TOKEN.search(tail)
            if amt and amt.start() > 0:
                tail = tail[:amt.start()].strip(" .-:|/\\")
            polished = _polish_name(tail)
            if polished:
                return polished
        # Layout 2: line-per-region. Walk forward through the
        # next few lines looking for a name-shaped line.
        for next_idx in range(idx + 1, min(idx + 4, len(lines))):
            next_line = lines[next_idx].strip()
            if not next_line:
                continue
            if _looks_like_template_boilerplate(next_line):
                continue
            polished = _polish_name(next_line)
            if polished and _looks_like_payee_name(polished):
                return polished
    return None


def _looks_like_template_boilerplate(line: str) -> bool:
    """Detect cheque template/boilerplate strings that appear
    between 'PAY' and the actual payee name in line-per-region
    OCR output. We don't want to misread "RUPEES ONE LACK ..."
    as the payee name.

    Returns True for: amount-in-words preambles (RUPEES, ONLY),
    template phrases (VALID UPTO, FOR NON-CASH, MULTI-CITY),
    signature-block labels (ease sign above, Proprietor), AND
    test-environment watermarks (NOT ON IMAGE — printed on
    every CTS UAT cheque face as a 'not for production'
    indicator; it kept leaking into the beneficiary field).
    """
    upper = line.upper()
    boilerplate_markers = (
        "RUPEES", "RUPES", "RUPESS", "RUPEE",
        "VALID", "VALIO",
        "FOR NON", "NON-HOME", "NON-CASH",
        "MULTI-CITY", "MULTI CITY", "CTS", "PAYABLE",
        "BRANCH", "BRANCHES",
        "PLEASE SIGN", "EASE SIGN", "SIGN ABOVE",
        "PROPRIETOR", "AUTHORISED", "AUTHORIZED",
        "ACCOUNT HOLDER", "SIGNATORY",
        # CTS UAT test environment watermarks
        "NOT ON IMAGE", "TEST CHEQUE", "SAMPLE",
        "BENEFICIARY 1", "BENEFICIARY 2", "BENEFICIARY 3",
        "EFICIARY",  # OCR truncation of "BENEFICIARY"
        # Bank identifier / header lines that aren't payees
        "BANK LTD", "BANK LIMITED", "BANK OF",
        "STATE BANK", "AXIS BANK", "ICICI BANK",
        "HDFC BANK", "HDFCBANK",
        "IFS CODE", "IFSC", "MICR CODE",
    )
    for marker in boilerplate_markers:
        if marker in upper:
            return True
    return False


def _looks_like_payee_name(s: str) -> bool:
    """A heuristic name-shape check. Payee names on Indian
    cheques are typically 3-60 chars, mostly letters, may have
    spaces / dots / ampersands / commas / hyphens. Must contain
    at least 3 consecutive alphabetic characters (a real word).

    Rejects: pure-digit lines, short noise tokens ('I1 <8',
    'Is', 'X'), URL-like strings, MICR fragments.
    """
    if not s or len(s) < 3 or len(s) > 60:
        return False
    # Must contain at least one run of 3+ consecutive
    # alphabetic chars (a real word). This rejects garbage
    # like 'I1 <8', 'Is 7', '9191 II' that have alpha chars
    # but no actual word.
    if not re.search(r"[A-Za-z]{3,}", s):
        return False
    # Reject lines where digits dominate — those are likely
    # MICR / cheque number / date lines that the boilerplate
    # filter missed.
    digit_count = sum(1 for ch in s if ch.isdigit())
    if digit_count > len(s) // 2:
        return False
    # Reject lines that are mostly special chars (a real name
    # has < 30% non-alphanumeric chars including spaces).
    special_count = sum(
        1 for ch in s
        if not ch.isalnum() and not ch.isspace()
    )
    if special_count > len(s) * 0.3:
        return False
    return True


def _find_cheque_no(lines: list[str]) -> str | None:
    """Find the cheque serial number in the body OCR text.

    Strategy (most-reliable → least):

      1. MICR-line parse. A CTS-2010 MICR strip line has 3+ long
         digit groups laid out as
         ``[cheque_no:6] [city:3][bank:3][branch:3] [account:N] [tc:2]``.
         When the dedicated focused MICR-strip OCR pass fails
         (blank/garbled strip crop — common on faded cheques), the
         FULL-FACE OCR usually still captures this line into the
         body text. Parsing it with `micr.parse_micr_text` pins the
         LEADING 6-digit group as the cheque serial. This is far
         more reliable than the generic last-token heuristic below,
         which routinely grabs a date-band fragment — e.g. it
         returned '162026' on the AKOLA JANATA cheque (June 2026)
         whose date band reads 21/06/2026, while the true serial
         '008064' sat in the MICR line the heuristic skipped.

      2. Anchor-relative parse. The 9-digit city-bank-branch run is
         unique to the MICR row, and the cheque serial is the
         6-digit run immediately to its LEFT. This rescues MICR rows
         that the full-face OCR split across tokens/lines (so the
         strict single-line gate in step 1 rejects them) — the
         common failure mode on faded/photocopied strips.

      3. Strict 6-digit serial fallback. CTS-2010 cheque serials are
         ALWAYS exactly 6 digits. 7-8-digit runs are account-number
         / deposit-stamp fragments, NEVER serials, so they are
         excluded here. (Operator-reported June 2026: the old
         "last 6-8 digit token" heuristic grabbed a misread 7-digit
         account fragment '6567000' as the serial while the true
         6-digit serial '017424' sat in the MICR row.) The MICR row
         sits at the BOTTOM of the cheque and the serial is its
         LEADING token, so we scan bottom-up and return the FIRST
         6-digit run of the bottom-most line that has one.

      4. Last-ditch 6-8-digit token. Only reached when NO 6-digit
         run exists anywhere (non-CTS / heavily-garbled strip).
    """
    # 1) MICR-line preference — a single clean line carrying the
    #    whole CTS row.
    from aakaar_caps.cheque import micr  # noqa: PLC0415
    for line in lines:
        long_groups = [g for g in re.findall(r"\d+", line) if len(g) >= 4]
        total_digits = sum(len(g) for g in long_groups)
        if len(long_groups) >= 3 and total_digits >= 14:
            parsed = micr.parse_micr_text(line)
            cn = parsed.get("cheque_no")
            if cn:
                return cn

    # 2) Anchor-relative: find the 9-digit city-bank-branch run and
    #    take the 6-digit run immediately to its left.
    for line in lines:
        runs = re.findall(r"\d+", line)
        for i, r in enumerate(runs):
            if len(r) == 9:
                for prev in reversed(runs[:i]):
                    if len(prev) == 6:
                        return prev
                break

    # 3) Strict 6-digit serial fallback — bottom-most line, leading
    #    6-digit run. Excludes 7-8-digit account/stamp fragments.
    for line in reversed(lines):
        six = re.findall(r"\b\d{6}\b", line)
        if six:
            return six[0]

    # 4) Last-ditch 6-8-digit token (no 6-digit run found above).
    digit_re = re.compile(r"\b(\d{6,8})\b")
    matches: list[str] = []
    for line in lines:
        for m in digit_re.finditer(line):
            matches.append(m.group(1))
    if not matches:
        return None
    # The MICR/bottom occurrence is the more reliable one.
    return matches[-1]


def _looks_like_ddmmyyyy(token: str) -> bool:
    """True when `token` is a plausible DDMMYYYY calendar date.

    The cheque date is printed in eight DDMMYYYY boxes (top-right) and
    OCRs as a single 8-digit run — e.g. '21062026' (21 Jun 2026). That
    run must NOT be mistaken for the amount-in-figures. We only treat a
    token as a date when it cleanly decomposes into a valid day (01-31),
    month (01-12) and a 1900-2099 year — so genuine 8-digit amounts
    like '21000000' (month '00' → invalid) survive."""
    digits = re.sub(r"[^0-9]", "", token)
    if len(digits) != 8:
        return False
    dd, mm, yyyy = int(digits[:2]), int(digits[2:4]), int(digits[4:])
    return 1 <= dd <= 31 and 1 <= mm <= 12 and 1900 <= yyyy <= 2099


def _is_date_box_line(line: str) -> bool:
    """True when a line is (part of) the DDMMYYYY date box — either the
    literal 'DDMMYYYY' / 'DD MM YYYY' label the recognizer emits beneath
    the boxes, or a line whose ONLY content is a plausible DDMMYYYY
    run. Lines matching this are skipped by the amount-in-figures
    extractor so the date never leaks into the amount field."""
    compact = re.sub(r"[^A-Za-z]", "", line).upper()
    if "DDMMYYYY" in compact or "DDMMYY" in compact:
        return True
    stripped = line.strip()
    return bool(stripped) and _looks_like_ddmmyyyy(stripped)


def _find_amount_in_figures(
    lines: list[str], *, decorated_only: bool = False,
) -> str | None:
    """Pick the highest-confidence numeric amount. Preference
    ladder, most-specific to least:

      1. `Rs. / ₹ / INR <number>` — explicit currency marker.
      2. `<number>/-` or `<number>/=` — Indian cheque box-
         terminator convention (`51060/-` means "₹51,060 only").
      3. A number that LOOKS LIKE a money amount (has a decimal,
         has commas), preferring tokens with both.
      4. Any 2–9-digit bare run (last-ditch — used when the user
         wrote the amount without ANY decoration, e.g. just
         '51060').

    The /- terminator pattern is the one that catches the
    real-world case where the customer wrote '51060/-' in the
    figures box: no commas, no decimal, no currency prefix —
    just the digits and the conventional terminator.
    """
    # Word-boundary anchored — without `\b` the bare `R` from
    # template text like 'UPTOR 1 CRORE' or 'PROPRIETOR 100'
    # falsely matched as "Rs <number>" and produced bogus
    # amounts like '1' / '100' on cheques where the real amount
    # box was empty / OCR-illegible. Require at LEAST 2 digits
    # in the captured number for the same reason — a real
    # cheque amount is never just '1' or '5'.
    #
    # Each "amount shape" has TWO branches:
    #   - Indian-grouped: 1,234.56 or 1,23,456 or 12,345
    #   - Bare digit run: 16141.00 or 51060 or 100000.00
    # The bare-digit branch is essential because many bank
    # cheques print the courtesy box as plain digits (e.g.
    # 'Rs.16141.00' with no comma). Without it, the [0-9]{2,3}
    # head was greedily capturing only the first 3 digits
    # ('161' from '16141.00') and dropping the rest, which
    # surfaced as a verification failure on a perfectly clean
    # cheque.
    # `\b` only guards the alphabetic markers (Rs / INR) — the ₹ glyph
    # is a non-word char, so a leading `\b` would FAIL to match
    # '₹250000' at the start of a line. Anchor ₹ without the boundary.
    explicit_re = re.compile(
        r"(?:\b(?:Rs\.?|INR)|₹)\s*"
        r"("
        r"[0-9]{1,3}(?:[,\s][0-9]{2,3})+(?:[.=][0-9]{1,2})?"
        r"|"
        r"[0-9]{2,9}(?:[.=][0-9]{1,2})?"
        r")",
    )
    # Indian '=' decimal separator. The amount box on most CTS cheques
    # is written as '47605=00' / '12,345=50' — the '=' is the
    # handwritten decimal point. Treat this as a STRONG amount signal
    # (an explicit paise part), second only to a Rs./₹-prefixed token.
    # Captures (whole, paise) so we can rebuild it as '47605.00'.
    eq_term_re = re.compile(
        r"\b("
        r"\d{1,3}(?:,\d{2,3})+"
        r"|"
        r"\d{2,9}"
        r")\s*=\s*(\d{2})\b",
    )
    # Indian /- box-terminator. Cover both '12345/-' and '12345 /'
    # and '12345/=' (rare but valid). Require ≥3 digits in the
    # bare branch so that stray '12/-' stamps, page labels like
    # 'cycle 02/-' etc. don't get picked up as the amount.
    slash_term_re = re.compile(
        r"\b("
        r"\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?"
        r"|"
        r"\d{3,9}(?:\.\d{1,2})?"
        r")\s*/\s*[-=_]?(?!\d)",
    )
    # "Decorated" = either comma-grouped (1,234.56) OR plain
    # digits with a decimal (16141.00). The decimal alone is
    # enough decoration to lift the token above the bare-plain
    # tier — operators rarely write account numbers or cheque
    # numbers with a trailing '.00'.
    bare_with_decoration_re = re.compile(
        r"\b("
        r"[0-9]{1,3}(?:,[0-9]{2,3})+(?:\.[0-9]{1,2})?"
        r"|"
        r"[0-9]{2,9}\.[0-9]{1,2}"
        r")\b",
    )
    # Last-ditch: any 3-9 digit run. Used only when no decorated
    # money token was found, so we don't accidentally pick up
    # cheque numbers (which are typically 6 digits without
    # commas/decimals — but if the user wrote a plain '51060'
    # in the amount box, this catches it). 3-digit minimum keeps
    # 2-digit page-label numbers (TC=30, Cycle=02) out of the
    # candidate pool — Indian cheque amounts are virtually never
    # below ₹100 in production CTS clearing.
    bare_plain_re = re.compile(r"\b(\d{3,9})\b")

    # MICR-row exclusion: a CTS MICR strip line has 3+ long digit
    # groups separated by spaces (e.g. "1976532 671002002 000563 29"
    # — cheque-no, city-bank-branch, ifsc-suffix, transaction-code).
    # If we scan those lines for amount tokens we'll pick up MICR
    # digits like "671002002" and surface them as the amount, which
    # is always wrong. We pre-filter lines that look like MICR rows
    # before applying the regex ladder.
    def _is_micr_line(line: str) -> bool:
        # MICR clear-band delimiter heuristic: the printed E-13B
        # symbols (transit ⑆ / on-us ⑈ / amount ⑇) OCR as stray
        # quotes / colons around the digit band, e.g.
        # '"678303"123002055:000208 30'. A line carrying such a
        # delimiter next to a long (>=10) digit run is the printed
        # MICR row — never the handwritten amount. Catch it FIRST so
        # the bare-digit fallback can't pick a MICR fragment (like
        # '00020830') as the amount when the amount box was illegible.
        if re.search(r'["\':\u2446-\u2449]', line):
            if len(re.sub(r"[^0-9]", "", line)) >= 10:
                return True
        digit_groups = [
            w for w in line.split()
            if w.isdigit() and len(w) >= 4
        ]
        total_digits = sum(len(g) for g in digit_groups)
        return len(digit_groups) >= 3 and total_digits >= 14

    # Bank branch-code / contact-info exclusion: SBI cheques have
    # '(06715) -KASARAGOD' in the bank header, HDFC cheques have
    # 'Tel: 22-67882000', and most have '470001-' style IFS / BSR
    # codes. These contain digit runs that aren't amounts — and
    # without filtering them the bare-digit fallback picks them.
    # Heuristic: line contains parens-wrapped digits OR a
    # 'Tel'/'IFS'/'MICR'/'Branch'/'CODE' label.
    def _is_bank_meta_line(line: str) -> bool:
        if re.search(r"\(\s*\d+\s*\)", line):
            return True
        upper = line.upper()
        # Pre-printed CTS form/template code in the cheque margin —
        # e.g. 'DDIPL-CTS 2010', 'DDPL-CTS-2010', 'SBINGRP/CTS-2015'.
        # The trailing 4-digit number is the CTS-2010 specification
        # year, NOT an amount. Without this we picked '2010' as the
        # amount on cheques whose handwritten box was illegible.
        if re.search(r"CTS[\s\-/]*\d{4}", upper):
            return True
        # Postal address line: a parenthesised 2-letter Indian state
        # code ('(HR)', '(MH)', '(GJ)', ...) marks an address whose
        # trailing 6-digit token is a PIN code, never the amount.
        if re.search(r"\([A-Z]{2}\)", line):
            return True
        meta_markers = (
            "TEL", "TEL:", "TEL.",
            "IFS", "IFSC", "MICR",
            "BSR", "SWIFT", "BIC",
            "PIN:", "PIN CODE",
            "BRANCH CODE", "BANK CODE",
            "ADD :", "ADDRESS",
            # Cheque/account-number lines carry 6-9 digit runs that
            # look like bare amounts under the last-ditch regex but
            # are NEVER the amount. Without this filter we'd pick
            # '17084954' (the cheque number) as the amount whenever
            # the operator wrote the figures without a decimal/comma.
            "CHEQUE NO", "CHQ NO", "CHEQUE NUMBER",
            "A/C NO", "ACCOUNT NO", "ACCOUNT NUMBER",
        )
        return any(m in upper for m in meta_markers)

    explicit: list[str] = []
    eq_term: list[str] = []
    slash_term: list[str] = []
    bare_dec: list[str] = []
    bare_plain: list[str] = []
    for line in lines:
        if _is_micr_line(line) or _is_bank_meta_line(line):
            continue
        # Date-box exclusion: the cheque date is printed in DDMMYYYY
        # boxes (top-right) and OCRs as a single 8-digit run plus a
        # literal 'DDMMYYYY' label. That run (e.g. '21062026') would
        # otherwise win the bare-digit amount tier. Skip lines that
        # carry the date-box marker entirely — the digits on / around
        # them are the DATE, never the amount.
        if _is_date_box_line(line):
            continue
        for m in explicit_re.finditer(line):
            explicit.append(m.group(1))
        for m in eq_term_re.finditer(line):
            eq_term.append(f"{m.group(1)}.{m.group(2)}")
        for m in slash_term_re.finditer(line):
            slash_term.append(m.group(1))
        for m in bare_with_decoration_re.finditer(line):
            bare_dec.append(m.group(1))
        for m in bare_plain_re.finditer(line):
            bare_plain.append(m.group(1))

    # Belt-and-braces: drop any candidate that is itself a plausible
    # DDMMYYYY date (covers cheques where the date run shares a line
    # with other text so the line-level skip above didn't catch it).
    eq_term = [v for v in eq_term if not _looks_like_ddmmyyyy(v)]
    bare_dec = [v for v in bare_dec if not _looks_like_ddmmyyyy(v)]
    bare_plain = [v for v in bare_plain if not _looks_like_ddmmyyyy(v)]

    # Apply the preference ladder. Each tier is independent — we
    # only fall through to the next when the previous one is
    # empty (avoids false-positive cheque numbers leaking into a
    # would-be amount field when explicit markers were present).
    # The '=' paise tier sits right behind an explicit Rs./₹ marker:
    # it's the canonical handwritten cheque-box form ('47605=00').
    pool = explicit or eq_term or slash_term or bare_dec
    if decorated_only:
        # Caller only trusts a STRUCTURALLY-decorated amount (currency
        # marker, '=' / '/-' paise terminator, decimal or comma group).
        # Used by the focused amount-box pass: a decorated token in the
        # cropped ₹ box is the amount with high confidence, whereas a
        # bare digit run there could be stray ink — so we skip the
        # last-ditch tier entirely and return None instead.
        if not pool:
            return None
        pool.sort(key=lambda v: (("." in v), ("," in v), len(v)), reverse=True)
        return _normalize_amount(pool[0])
    if not pool:
        # Last-ditch bare plain digit run. Only consider tokens
        # in a length range that makes sense for a cheque amount
        # (avoid 6-digit-exactly which is almost always a cheque
        # number — operators write amounts as ≥3 digits and very
        # rarely exactly 6). Also cap the upper bound at 8 digits
        # (max plausible amount = ₹99,999,999 = 8 digits) — 9-digit
        # runs are almost always MICR garbage that survived the
        # MICR-line pre-filter (e.g. a transaction reference
        # printed elsewhere).
        # Last-ditch bare digit run. We are deliberately conservative
        # here: when the amount box was illegible, returning None
        # ("amount not located") is far better than a confidently-wrong
        # number. A bare run only qualifies as the amount when it is:
        #   • 3-8 digits (₹100 .. ₹99,999,999 — the plausible range),
        #   • NOT leading-zero ('0271' / '00020830' are OCR noise or
        #     MICR/reference fragments, never a handwritten amount).
        # We no longer blanket-reject 6-digit runs: real cheque amounts
        # are routinely 6 digits (₹1,00,000 .. ₹9,99,999), and an
        # imperfect OCR read of the amount box (e.g. '18668' read as
        # '189981') is far more useful to surface than to discard. The
        # 6-digit non-amounts (cheque numbers, PIN codes) are removed
        # upstream by the MICR-row, CTS-form-code and postal-address
        # line filters instead of here.
        def _plausible_bare(v: str) -> bool:
            return 3 <= len(v) <= 8 and not (len(v) > 1 and v[0] == "0")

        pool = [v for v in bare_plain if _plausible_bare(v)]
        if not pool:
            return None
    # Prefer tokens that LOOK like full amounts (decimal + commas).
    pool.sort(key=lambda v: (("." in v), ("," in v), len(v)), reverse=True)
    return _normalize_amount(pool[0])


_AMOUNT_NUMBER_WORDS: frozenset[str] = frozenset({
    # Cardinal digit names
    "zero", "one", "two", "three", "four", "five", "six",
    "seven", "eight", "nine", "ten", "eleven", "twelve",
    "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
    "eighteen", "nineteen", "twenty", "thirty", "forty",
    "fifty", "sixty", "seventy", "eighty", "ninety",
    # Magnitudes — Indian (lakh/lac/crore) + Western
    "hundred", "thousand", "million", "billion",
    "lakh", "lac", "lakhs", "lacs", "crore", "crores",
    # OCR misreads of "lack" → "lakh", "thousnd" → "thousand"
    "lack", "lakhs", "thousnd", "thoushand", "hundrd",
    # Connectives that legitimately appear in amount-in-words
    "and", "plus", "rupees", "rupee", "rs",
    # The terminator itself
    "only",
})


def _count_amount_number_words(text: str) -> int:
    """Count tokens in `text` that are recognised amount-in-words
    vocabulary (including connectives 'and' / 'rupees' and the
    'only' terminator). Used to decide whether an OCR'd line
    belongs to the amount-in-words band."""
    tokens = [t.lower() for t in re.split(r"[^A-Za-z]+", text) if t]
    return sum(1 for t in tokens if t in _AMOUNT_NUMBER_WORDS)


def _is_amount_band_boundary(line: str) -> bool:
    """Return True when `line` is a known cheque section break
    (signature, MICR, address, account-no, contact info, OR a
    template-stamp line like 'VALID FOR THREE MONTHS ONLY').
    When walking forward/backward from a 'Rupees' or 'Only'
    anchor we STOP at these — they're not part of the amount-in-
    words band and slurping them produces garbage (e.g. the
    template stamp 'VALID FOR THREE MONTHS ONLY' showing up
    in the amount-words field; signature-block 'PROPRIETOR'
    polluting the amount band)."""
    upper = line.upper()
    boundary_markers = (
        # Signature block
        "PROPRIETOR", "AUTHORISED", "AUTHORIZED",
        "SIGNATORY", "SIGNATURE",
        "PLEASE SIGN", "EASE SIGN", "SIGN ABOVE",
        "ACCOUNT HOLDER",
        # Bank / MICR / contact
        "IFSC", "IFS CODE", "MICR", "BSR", "SWIFT", "BIC",
        "BRANCH CODE", "BANK CODE",
        "TEL:", "TEL.", "TEL ",
        # Account / cheque metadata
        "A/C NO", "ACCOUNT NO", "ACCOUNT NUMBER",
        "DATE:", "DATED",
        # Test-watermark stamp
        "NOT ON IMAGE",
        # Template stamps that legitimately end in 'ONLY' but
        # are NOT the amount-in-words. These were misread as the
        # amount on real production cheques because Strategy 2
        # (anchor-on-Only) found their terminator and the
        # backward-walk had nothing else to grab.
        "VALID FOR", "VALID UPTO", "VALID UPTOR", "VALIO UPTO",
        "MONTHS ONLY", "MONTH ONLY", "DAYS ONLY", "DAY ONLY",
        "NON-CASH TRANSACTION", "NON CASH TRANSACTION",
        "NON-HOME BRANCH", "NON HOME BRANCH",
        "CROSSED ACCOUNT PAYEE",
        "ACCOUNT PAYEE ONLY", "A/C PAYEE ONLY",
        # MICR-layout marker (the "DDMMY" / "DDMMYYYY" template
        # label printed above the date band)
        "DDMMYYYY", "DDMMY", "MMYYYY",
    )
    return any(m in upper for m in boundary_markers)


# ---------------------------------------------------------------------------
# RUPEES marker — permissive matcher
# ---------------------------------------------------------------------------
# OCR consistently mangles the printed 'Rupees' template label.
# Real production examples we've seen:
#   * 'RUPEES' (clean)
#   * 'Rupees' (clean)
#   * 'Rupess' / 'Rupes' / 'Rupee' (single-letter drops)
#   * 'RUPEEST' — adjacent 'T' from a 'STAMP' watermark got
#     OCR'd as part of the same token (real HDFC case)
#   * 'RUPEEST', 'RUPEESt', 'RUPEEStt' — any 1-3 trailing letters
#   * 'Rs' (short form, with or without trailing dot)
#
# We accept any of these as the 'Rupees' anchor for Strategy 1.
# The trailing letters are stripped from the tail by the caller's
# leading-punctuation cleanup, so 'RUPEEST Sintlees' yields
# tail='Sintlees' (the 'T' is consumed by the regex, not by the
# tail).
_RUPEES_MARKER_RE = re.compile(
    # 'Rupe+s*' matches 'Rupe', 'Rupee', 'Rupees', 'Rupeess', 'Rupeeesss', etc.
    # '[a-zA-Z]{0,3}' tail allows up to 3 trailing OCR-noise letters
    # (the 'T' in 'RUPEEST', the 'a' in 'Rupeesa', etc.) to be
    # consumed by the regex instead of leaking into the amount-words
    # tail. The combination is the union of the previous strict
    # alternatives plus the fuzzy trailing-letter pattern.
    r"\b(?:Rupe+s*|Rs)[a-zA-Z]{0,3}",
    re.IGNORECASE,
)


def _find_amount_in_words(lines: list[str]) -> str | None:
    """The 'Rupees ...' line is the human-readable amount. Cheques
    in production carry this band in many layouts because OCR
    output and cheque template designs vary widely. We handle:

      A. INLINE single line — 'Rupees Ten Thousand Only'.
      B. INLINE two-line — 'Rupees One Lac Four Thousand
         Three\\nHundred Sixty One Only' (typical Paddle / Easy
         output when the handwriting wraps to a second printed
         line).
      C. APPLE-VISION LINE-PER-REGION — 'Rupees\\nOne Lac Four
         Thousand Three\\nHundred Sixty One Only' (Vision splits
         each text observation onto its own line).
      D. NO RUPEES LABEL — just '<words>\\n<more words> Only'
         (the OCR detected the handwriting but missed the printed
         'Rupees' template label — common on faded cheques
         where the printed text is lighter than the handwriting).
      E. ONLY ALONE — '<words>\\n<more words>\\nOnly' (rare;
         operator wrote 'Only' on its own line OR the OCR
         split the final word off).

    Strategy: anchor on 'Rupees' (Strategy 1) OR 'Only'
    (Strategy 2), then walk OUTWARD up to 4 lines collecting
    only lines that contain amount-vocabulary words AND don't
    cross a section boundary. This way we never accidentally
    slurp the signature block or the date line into the amount-
    in-words field.
    """
    # Use the permissive `_RUPEES_MARKER_RE` (matches RUPEEST,
    # RUPESS, RUPESST, etc. — see comment near its definition).
    rupees_re = _RUPEES_MARKER_RE
    only_re = re.compile(r"\bOnly\b", re.IGNORECASE)

    # Strategy 1: anchor on the printed 'Rupees' label and walk
    # FORWARD until we either hit 'Only' (the canonical
    # terminator) or run out of amount-vocabulary lines.
    #
    # IMPORTANT — collect candidates from EVERY rupees-marked line
    # instead of returning the first one. The composite text seen
    # here is `apple_vision_text + "\n" + paddle_text` (see the
    # composite assembly in `run_ocr_detail_oriented` ↔
    # `_extract_front_fields` call site). When Apple Vision misses
    # the second handwriting line of a wrap-around amount, its
    # 'Rupees' marker walks forward into an account-number
    # boundary and returns only line 1. The SAME 'Rupees' marker
    # in Paddle's appended text often has both lines (different
    # region segmentation) and yields a richer answer — but the
    # old "return first match" code never gave that second
    # candidate a chance. Operator-observed on the AKOLA JANATA
    # Co-op cheque, June 2026 (apple_vision returned
    # 'Eighekeen Housend Six' only; paddle had both lines).
    strategy1_candidates: list[str] = []
    for idx, line in enumerate(lines):
        m = rupees_re.search(line)
        if not m:
            continue
        if _is_amount_band_boundary(line):
            # The 'Rupees' marker landed in a HEADER-like line
            # (e.g. 'TOTAL RUPEES IFSC ...'). But: if the same
            # line ALSO contains 'Only', the real amount is in
            # the tail and the boundary marker is trailing junk
            # we'll truncate away — keep going. Only skip when
            # the boundary marker appears WITHOUT an Only on the
            # same line (then it really IS a header, not an
            # amount line with trailing junk).
            if not only_re.search(line):
                continue
        # Strip leading punctuation / template noise after the
        # 'Rupees' marker (':', '-', '.', '/', etc.) so the tail
        # starts at the first real handwriting character. The
        # permissive RUPEES regex consumes up to 3 trailing
        # letters (the OCR-noise letters attached to the marker)
        # so the tail is already free of those; we just need to
        # strip surrounding whitespace and punctuation.
        tail = line[m.end():].lstrip(" :.,;\\-_|/\t").rstrip()
        parts: list[str] = []
        if tail:
            parts.append(tail)
        terminator_found = bool(parts and only_re.search(parts[0]))
        if not terminator_found:
            # Walk forward up to 4 lines. We accept a line if:
            #   - It has 'Only' (terminator — include + STOP),
            #   - It has at least one amount-vocab word (keep
            #     walking),
            #   - The line IMMEDIATELY after the Rupees marker
            #     when tail was empty (we always trust the first
            #     handwritten line even if OCR garbled the
            #     individual tokens — better recall than insisting
            #     on a recognisable word).
            # We STOP at: boundary lines, another Rupees marker,
            # OR the first non-vocab line that isn't the
            # immediate next line.
            for offset, next_idx in enumerate(
                range(idx + 1, min(idx + 5, len(lines))),
                start=1,
            ):
                next_line = lines[next_idx].strip()
                if not next_line:
                    continue
                if _is_amount_band_boundary(next_line):
                    break
                if rupees_re.search(next_line) and next_idx > idx + 1:
                    break
                has_only = bool(only_re.search(next_line))
                has_vocab = _count_amount_number_words(next_line) >= 1
                # Trust the FIRST handwriting line even if OCR
                # mangled it (Apple Vision sometimes returns
                # 'ne Lack Foul Thsd' for the handwritten band).
                is_first_after_label = (
                    offset == 1 and not parts
                )
                # 2-line handwriting continuation. When the Rupees-
                # line tail already gave us vocab but NO 'Only'
                # terminator, the amount has almost certainly
                # wrapped to a second handwritten line that owns
                # the terminator. Cursive handwriting OCR routinely
                # produces zero-vocab garbage on the wrap line
                # ('tAerAchModiadu Sixtr Eighd Rr andy' for
                # 'Hundred Sixty Eight Rs only' — operator-observed,
                # June 2026), so insisting on vocab here drops the
                # continuation entirely. Boundary markers (DATE,
                # ACCOUNT NO, AUTHORISED, etc.) still gate us from
                # slurping the wrong band.
                trust_continuation = (
                    offset == 1
                    and bool(parts)
                    and not terminator_found
                )
                if not (
                    has_only or has_vocab or is_first_after_label
                    or trust_continuation
                ):
                    break
                parts.append(next_line)
                if has_only:
                    terminator_found = True
                    break
        if not parts:
            continue
        joined = " ".join(parts).strip()
        # Truncate at 'Only' (inclusive of the word — canonical
        # cheque format).
        only_m = only_re.search(joined)
        if only_m:
            joined = joined[:only_m.end()].strip()
        # Require the joined result to have at least 1 amount-
        # vocab word — else it's garbage. (The 'Rupees' marker
        # itself counts as vocab so a single-word 'Rupees Ten'
        # qualifies.)
        if joined and _count_amount_number_words(joined) >= 1:
            strategy1_candidates.append(joined)

    if strategy1_candidates:
        # Pick the BEST Strategy-1 candidate. Ranking, in priority
        # order: (1) has 'Only' terminator (canonical form), (2)
        # more amount-vocabulary tokens (richer reads), (3) longer
        # string (last-resort tiebreak when vocab counts tie).
        # A multi-engine composite text can produce several rupees
        # markers each anchoring a different walk; the cleanest
        # (most-complete) walk wins.
        def _rank_candidate(c: str) -> tuple[int, int, int]:
            has_only = 1 if only_re.search(c) else 0
            vocab = _count_amount_number_words(c)
            return (has_only, vocab, len(c))
        strategy1_candidates.sort(key=_rank_candidate, reverse=True)
        return strategy1_candidates[0]

    # Strategy 2: anchor on the 'Only' terminator and walk
    # BACKWARD up to 3 lines collecting amount-vocab lines. This
    # catches Layout D / E where OCR missed the printed 'Rupees'
    # label entirely (faded template) but caught the handwritten
    # words. Without backward-walk we'd return only the final
    # line's words and lose the first line of the amount.
    for idx in range(len(lines)):
        line = lines[idx]
        m = only_re.search(line)
        if not m:
            continue
        only_line = line[: m.end()].strip()
        if _is_amount_band_boundary(line):
            continue
        before_parts: list[str] = []
        for j in range(idx - 1, max(idx - 4, -1), -1):
            prev_line = lines[j].strip()
            if not prev_line:
                continue
            if _is_amount_band_boundary(prev_line):
                break
            if rupees_re.search(prev_line):
                # Found the 'Rupees' label on a prior line
                # (Strategy 1 should have caught this — but
                # might have skipped because of a boundary). Take
                # this line's tail and stop walking back.
                rm = rupees_re.search(prev_line)
                prev_tail = prev_line[rm.end():].strip()
                if prev_tail:
                    before_parts.insert(0, prev_tail)
                break
            # Only include lines that look like amount-words —
            # i.e. have at least one vocab token. This stops us
            # from including the payee name or template noise.
            if _count_amount_number_words(prev_line) < 1:
                break
            before_parts.insert(0, prev_line)
        candidate = " ".join(before_parts + [only_line]).strip()
        # Final acceptance: candidate must have at least 2
        # distinct vocab tokens (so a stray 'TOTAL ONLY' on
        # some other line doesn't false-trigger).
        if _count_amount_number_words(candidate) >= 2:
            return candidate
    return None


def _find_account_no(
    lines: list[str],
    *,
    dom_account_hint: str | None = None,
) -> str | None:
    """Account numbers on the back are usually labelled
    'A/C', 'A/c No', 'Account', etc. Pick the longest 9+ digit run
    in the file, with a strong preference for one that's adjacent to
    such a label.

    `dom_account_hint`: the bank's authoritative account number.
    When supplied we change the picker from "longest wins" to
    "best DOM match wins, longest as tiebreaker". This addresses
    the production failure mode where the back OCR captures BOTH
    the printed deposit-stamp account number AND a longer
    transaction-reference line — under longest-wins the
    transaction reference (e.g. 17 digits) beats the real account
    (e.g. 14 digits) and the rule FAILs on the wrong candidate.
    """
    labelled_re = re.compile(
        r"(?:A\s*/?\s*C(?:\s*No\.?)?|Acct\.?|Account\s*No\.?)[\s:\-]*([0-9][\d\s-]{8,})",
        re.IGNORECASE,
    )
    bare_re = re.compile(r"\b(\d[\d\s-]{8,})\b")

    labelled: list[str] = []
    bare: list[str] = []
    for line in lines:
        for m in labelled_re.finditer(line):
            labelled.append(m.group(1))
        for m in bare_re.finditer(line):
            bare.append(m.group(1))

    pool = labelled or bare
    if not pool:
        return None

    def candidate_digits(raw: str) -> str:
        return re.sub(r"\D", "", raw)

    # Normalise each candidate to its digits-only form, then keep
    # only those in the plausible-account-number range (9-18
    # digits). 30-char OCR garble would otherwise pollute the
    # candidate pool.
    digit_pool: list[str] = []
    for raw in pool:
        d = candidate_digits(raw)
        if 9 <= len(d) <= 18:
            digit_pool.append(d)
    if not digit_pool:
        return None

    # No DOM hint → legacy behaviour (longest wins).
    if not dom_account_hint:
        digit_pool.sort(key=len, reverse=True)
        return digit_pool[0]

    # DOM-aware scoring. Rank order (highest = best):
    #   3 — exact equality with the DOM hint
    #   2 — one is a suffix / prefix of the other (routing-code
    #       prefixing is the common cheque-system convention)
    #   1 — DOM digits appear as a contiguous substring of the
    #       candidate (or vice versa)
    #   0 — no overlap
    # Among equally-scored candidates we keep the longest
    # (preserves legacy behaviour when the DOM hint can't
    # disambiguate, e.g. when the back OCR is too garbled for
    # any candidate to match the system value).
    dom_digits = re.sub(r"\D", "", dom_account_hint or "")

    def score(candidate: str) -> int:
        if not dom_digits:
            return 0
        if candidate == dom_digits:
            return 3
        if (
            candidate.endswith(dom_digits)
            or dom_digits.endswith(candidate)
            or candidate.startswith(dom_digits)
            or dom_digits.startswith(candidate)
        ):
            return 2
        if dom_digits in candidate or candidate in dom_digits:
            return 1
        return 0

    digit_pool.sort(key=lambda d: (score(d), len(d)), reverse=True)
    return digit_pool[0]


# ---------- small helpers -------------------------------------------------


def _normalize_amount(token: str) -> str:
    """Strip stray whitespace inside the number, leave commas and
    decimals as-is so the UI can display the cheque-typical form
    (e.g. '10,000.00' rather than '10000.00'). The Indian '=' decimal
    separator ('47605=00') is normalised to a '.' so downstream
    amount comparison sees a standard '47605.00'."""
    token = re.sub(r"\s+", "", token)
    return token.replace("=", ".")


def _polish_name(s: str) -> str:
    """Trim trailing OCR garbage from a name line (commas, dots,
    long underscore runs)."""
    s = re.sub(r"[._\-:|/\\]{2,}.*$", "", s).strip()
    s = s.strip(" .-:|/\\")
    return s


# ---------- DOM ↔ OCR cross-validation ------------------------------------
#
# The CTS UAT viewer shows the bank's parsed cheque fields in a panel
# under the image (Beneficiary / Account No / Cheque No / Amount).
# Those values are authoritative — the bank entered them. We treat OCR
# as a SECOND opinion: if the OCR-derived text agrees with the panel,
# the cheque is verified end-to-end; if it disagrees, the operator
# should look at the image because either the bank made a data-entry
# slip or the cheque image doesn't match the record.
#
# The match logic is intentionally tolerant — OCR is noisy and the two
# values rarely match byte-for-byte. We normalize aggressively (strip
# spaces, lowercase) and then compute SequenceMatcher similarity. The
# match threshold differs per field: numeric fields (cheque no,
# amount, account no) need digit-equivalence; text fields tolerate
# ~85% similarity for OCR error margin.


# Keys we accept on the DOM dict (matched case-insensitively, with
# spaces / punctuation stripped). Different cheque types surface
# slightly different labels — we map them all to the same canonical
# field name.
_DOM_ALIASES: dict[str, tuple[str, ...]] = {
    "beneficiary": ("beneficiary", "payee", "beneficiary1"),
    "cheque_no": ("chequeno", "cheque", "chq", "chequenumber"),
    "amount": ("amount", "chequeamount", "amt"),
    "account_no": ("accountno", "account", "accno", "ano", "acno"),
}


def _normalize_text(s: str | None) -> str:
    if not s:
        return ""
    s = s.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", s.strip()).lower()


def _normalize_digits(s: str | None) -> str:
    """Keep digits only — used for cheque no / account no comparisons
    where punctuation and stray spaces are pure OCR/formatting noise."""
    if not s:
        return ""
    return re.sub(r"\D", "", s)


def _normalize_amount_for_compare(s: str | None) -> str:
    """Strip the rupee sign / commas / whitespace and zero-pad to a
    single decimal-point form so '51,060.00' and '51060' compare
    equal. We deliberately drop trailing '.00' so '51060' matches
    '51060.00' too."""
    if not s:
        return ""
    cleaned = re.sub(r"[^0-9.]", "", s)
    if "." in cleaned:
        whole, _, frac = cleaned.partition(".")
        frac = frac.rstrip("0")
        return whole + ("." + frac if frac else "")
    return cleaned


def _similarity(a: str, b: str) -> float:
    """SequenceMatcher ratio with empty-string handling. Both empty
    → 0.0 (not a match — nothing to verify); one empty → 0.0."""
    if not a or not b:
        return 0.0
    from difflib import SequenceMatcher  # noqa: PLC0415
    return SequenceMatcher(None, a, b).ratio()


def _pick_dom_value(dom: dict[str, Any], canonical: str) -> str | None:
    """Find `canonical`'s value in the DOM scrape, accepting any of
    the known aliases. Match is case-insensitive after stripping
    spaces and punctuation."""
    if not dom:
        return None
    aliases = set(_DOM_ALIASES.get(canonical, (canonical,)))
    # Normalize each DOM key the same way we normalized the aliases.
    for k, v in dom.items():
        key = re.sub(r"[^a-z0-9]", "", str(k).lower())
        if key in aliases and v:
            return str(v)
    return None


def _compare_field(
    canonical: str,
    dom_value: str | None,
    ocr_value: str | None,
) -> dict[str, Any]:
    """Per-field cross-check. Returns the dict shape the capability's
    `_ChequeFieldValidation` schema expects."""
    if not dom_value and not ocr_value:
        return {
            "dom_value": None,
            "ocr_value": None,
            "similarity": 0.0,
            "match": False,
            "note": "neither side reported a value",
        }
    if not dom_value:
        return {
            "dom_value": None,
            "ocr_value": ocr_value,
            "similarity": 0.0,
            "match": False,
            "note": "no DOM value to compare against",
        }
    if not ocr_value:
        return {
            "dom_value": dom_value,
            "ocr_value": None,
            "similarity": 0.0,
            "match": False,
            "note": "OCR did not extract this field",
        }

    if canonical in ("cheque_no", "account_no"):
        dom_n = _normalize_digits(dom_value)
        ocr_n = _normalize_digits(ocr_value)
        # For numeric IDs we accept SUBSTRING match in either
        # direction — OCR sometimes misses leading zeros, the DOM
        # sometimes carries a routing prefix the cheque doesn't.
        if dom_n and ocr_n and (dom_n in ocr_n or ocr_n in dom_n):
            sim = 1.0
        else:
            sim = _similarity(dom_n, ocr_n)
        match = sim >= 0.95
    elif canonical == "amount":
        dom_n = _normalize_amount_for_compare(dom_value)
        ocr_n = _normalize_amount_for_compare(ocr_value)
        sim = 1.0 if (dom_n and ocr_n and dom_n == ocr_n) else _similarity(dom_n, ocr_n)
        match = sim >= 0.95
    else:
        # Text — beneficiary etc. SequenceMatcher on the lowercased,
        # whitespace-collapsed form.
        dom_n = _normalize_text(dom_value)
        ocr_n = _normalize_text(ocr_value)
        sim = _similarity(dom_n, ocr_n)
        match = sim >= 0.80

    return {
        "dom_value": dom_value,
        "ocr_value": ocr_value,
        "similarity": round(sim, 3),
        "match": bool(match),
        "note": None,
    }


# ---------- presence-based DOM verification -------------------------------
#
# Operator workflow on the live page: read the bottom-panel fields
# (Account No / Cheque No / Amount / Beneficiary / City / Bank /
# Branch / TC / Beneficiary 1/2/3 — the bank's authoritative values),
# then verify EACH ONE actually appears on the cheque image we just
# captured.
#
# The validation isn't "did OCR extract this field?" (that depends on
# regex luck on noisy scans) — it's "did the value the bank typed
# into the panel show up anywhere in the cheque image OCR text?" That
# answers the operator's real question: does the image match the data?
#
# Matching is layered:
#   1. Exact substring (case + whitespace insensitive) — fastest, best.
#   2. Digit-only substring — for numeric fields where OCR garbled
#      punctuation. Catches "50200 100 315661" vs "50200100315661".
#   3. Fuzzy substring — SequenceMatcher over a sliding window of the
#      OCR text for textual fields where OCR mis-read a letter or two.


_PRESENCE_THRESHOLD = 0.85  # fuzzy similarity ratio for text fields


def _normalize_for_search(s: str) -> str:
    """Collapse whitespace, strip currency / punctuation, lower-case.
    Used on both the DOM value and the OCR text so the substring
    check is robust to formatting differences."""
    if not s:
        return ""
    s = s.replace("\u00a0", " ").lower()
    # Normalise common OCR confusions:
    #   • non-breaking-hyphen / minus → ascii hyphen
    #   • runs of whitespace          → single space
    s = re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]", "-", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _digits_only(s: str) -> str:
    return re.sub(r"\D", "", s or "")


# OCR letter ↔ digit confusions, applied ONLY to tokens that already
# contain digits (so plain words don't get butchered into digit-soup).
#
# Each letter maps to a TUPLE of plausible digit interpretations
# because a single glyph can be confused with more than one digit
# depending on the font and the OCR engine's preprocessing. For
# example, an uppercase `L` printed in a CTS-2010 cheque font is
# usually a misread `1` (both are tall vertical strokes), but
# when the cheque uses an open-top `4` (no top crossbar reaching
# all the way left) the same `L` is more likely a misread `4`.
# We try EVERY substitution variant — if ANY produces a digit
# string that contains the DOM digits, the rule treats it as a
# match.
#
# Production regression that motivated this: the cheque number
# `143144` came through OCR as `143iL4` (`i` for the first `1`,
# `L` for the second `4`). digits-only stripped it to `1434`,
# never matched. With `L → 4` in the variant set the token
# rewrites to `143144` and matches cleanly.
_OCR_LETTER_TO_DIGIT_VARIANTS: dict[str, tuple[str, ...]] = {
    "i": ("1",), "I": ("1",),
    "l": ("1",), "L": ("1", "4"),
    "|": ("1",), "!": ("1",),
    "o": ("0",), "O": ("0",),
    "D": ("0",), "Q": ("0",),
    "s": ("5",), "S": ("5",),
    "z": ("2",), "Z": ("2",),
    "B": ("8", "6"),
    "G": ("6",), "b": ("6",),
    "T": ("7",),
    "g": ("9",), "q": ("9",),
}

_OCR_TOLERANT_TOKEN_RE = re.compile(r"\S+")


def _ocr_letter_digit_variants(text: str) -> list[str]:
    """Return every distinct digit-only string that can be derived
    from `text` by substituting OCR letter-↔-digit confusions
    inside tokens that already contain digits. The first entry is
    always the plain digits-only string (no substitution) so
    callers can compare in priority order if they care which tier
    matched.

    Bounded expansion: we cap the variant set at 16 entries to
    keep complex haystacks (many ambiguous letters) from blowing
    up combinatorially. In practice cheques rarely have more than
    one or two ambiguous letters inside a single digit token, so
    the cap is well above the realistic working set."""

    if not text:
        return [""]

    out: set[str] = set()

    def emit_variant(transform_choices: dict[str, str]) -> None:
        if len(out) >= 16:
            return

        def fix_token(m: re.Match[str]) -> str:
            token = m.group(0)
            if not any(ch.isdigit() for ch in token):
                return token
            return token.translate(str.maketrans(transform_choices))

        rewritten = _OCR_TOLERANT_TOKEN_RE.sub(fix_token, text)
        out.add(re.sub(r"\D", "", rewritten))

    # Pick a single representative variant per letter at a time —
    # we don't enumerate the full Cartesian product (that would
    # explode for cheques with many ambiguous letters). Instead
    # we walk each letter and try EACH of its plausible
    # substitutions while pinning every other letter to its
    # primary mapping. This catches the common case ("one
    # ambiguous letter in the cheque number") in O(letters)
    # variants while keeping the search bounded.
    primary = {
        letter: variants[0]
        for letter, variants in _OCR_LETTER_TO_DIGIT_VARIANTS.items()
    }
    emit_variant(primary)
    for letter, variants in _OCR_LETTER_TO_DIGIT_VARIANTS.items():
        if len(variants) <= 1:
            continue
        for variant in variants[1:]:
            choices = dict(primary)
            choices[letter] = variant
            emit_variant(choices)

    # Plain digits-only (no substitution) first, then the
    # substitution variants, deduplicated.
    plain = re.sub(r"\D", "", text)
    ordered: list[str] = [plain]
    for v in out:
        if v != plain and v not in ordered:
            ordered.append(v)
    return ordered


def _ocr_letter_digit_text_variants(text: str) -> list[str]:
    """Like `_ocr_letter_digit_variants` but returns the REWRITTEN
    TEXT (digit boundaries preserved) for each letter↔digit
    substitution variant, instead of the digit-collapsed string.

    Callers use this to run a standalone-number search that
    respects digit boundaries — so a value can still be rescued
    through OCR letter confusions (e.g. `143iL4` → `143144`)
    WITHOUT the boundary-blind full-collapse that lets a short
    value match coincidentally inside a longer digit run."""
    if not text:
        return [""]
    out: list[str] = [text]
    seen: set[str] = {text}
    primary = {
        letter: variants[0]
        for letter, variants in _OCR_LETTER_TO_DIGIT_VARIANTS.items()
    }

    def emit(choices: dict[str, str]) -> None:
        if len(out) >= 16:
            return

        def fix_token(m: re.Match[str]) -> str:
            token = m.group(0)
            if not any(ch.isdigit() for ch in token):
                return token
            return token.translate(str.maketrans(choices))

        rewritten = _OCR_TOLERANT_TOKEN_RE.sub(fix_token, text)
        if rewritten not in seen:
            seen.add(rewritten)
            out.append(rewritten)

    emit(primary)
    for letter, variants in _OCR_LETTER_TO_DIGIT_VARIANTS.items():
        if len(variants) <= 1:
            continue
        for variant in variants[1:]:
            choices = dict(primary)
            choices[letter] = variant
            emit(choices)
    return out


def _digit_run_aligned_present(dom_digits: str, haystack: str) -> bool:
    """True when `dom_digits` appears in `haystack` as a number that
    touches a digit-run BOUNDARY on at least one side — i.e. it is
    the whole run, or a prefix of it, or a suffix of it — tolerating
    a little OCR noise (≤2 non-digit, non-newline chars) BETWEEN
    consecutive digits.

    What it accepts:
      * standalone   "  000017  "          (both sides non-digit)
      * prefix       "5020 0100 3156 61"   (run starts with the value)
      * suffix       "01378781" for 378781 (run ends with the value)

    What it REJECTS — the field-reported false positive:
      * interior     "4000001785213" for 000017 (digits on BOTH sides)

    A short, zero-padded value matching coincidentally in the MIDDLE
    of a longer digit run (an account number, amount, or MICR field)
    is never a real field match — it must not override the real
    structured read with a bogus PASS. Prefix/suffix alignment is
    kept because the cheque-number rule itself treats a value that
    is a prefix/suffix of the printed MICR number as a match."""
    if not dom_digits or len(dom_digits) < 4 or not haystack:
        return False
    body = r"[^\d\n]{0,2}".join(re.escape(d) for d in dom_digits)
    # Prefix-aligned: non-digit (or start) immediately before the run.
    if re.search(r"(?<!\d)" + body, haystack):
        return True
    # Suffix-aligned: non-digit (or end) immediately after the run.
    return re.search(body + r"(?!\d)", haystack) is not None


def _fuzzy_substring(needle: str, haystack: str) -> float:
    """Best SequenceMatcher ratio achievable by aligning `needle`
    against any substring of `haystack` of the same length. Returns
    0.0 when either input is too short to compare meaningfully.

    Step size is 1 — SequenceMatcher is fast for the short strings
    we feed it (cheque-OCR haystacks rarely exceed 2 KB, needles
    are < 60 chars), and skipping positions causes false negatives
    when the alignment falls between window stops (e.g. needle
    "JAY SHIVSAKTHI TRADERS" appearing at offset 4 in a 30-char
    haystack would never get scored under a half-needle step)."""
    needle = needle.strip()
    haystack = haystack.strip()
    if len(needle) < 4 or not haystack:
        return 0.0
    from difflib import SequenceMatcher  # noqa: PLC0415
    if len(needle) >= len(haystack):
        return SequenceMatcher(None, needle, haystack).ratio()
    best = 0.0
    for i in range(0, len(haystack) - len(needle) + 1):
        window = haystack[i : i + len(needle)]
        ratio = SequenceMatcher(None, needle, window).ratio()
        if ratio > best:
            best = ratio
            if best >= 0.99:
                return best
    return best


def _check_presence(
    dom_value: str | None,
    ocr_text: str,
    *,
    numeric: bool,
) -> dict[str, Any]:
    """Decide whether `dom_value` is present in `ocr_text`. Returns
    the dict shape the API's CtsChequePresenceCheck schema expects."""
    if not dom_value:
        return {
            "dom_value": None,
            "present": False,
            "match_kind": None,
            "similarity": 0.0,
            "note": "no DOM value to verify",
        }
    if not ocr_text:
        return {
            "dom_value": dom_value,
            "present": False,
            "match_kind": None,
            "similarity": 0.0,
            "note": "OCR text empty — image had no readable text",
        }

    norm_dom = _normalize_for_search(dom_value)
    norm_ocr = _normalize_for_search(ocr_text)
    dom_digits = _digits_only(dom_value)

    # A PURE-digit numeric value (cheque no, account no, the
    # paise-stripped amount variant, MICR sub-codes) must be matched
    # as a STANDALONE number — never as a bare substring — so a
    # short, zero-padded value like `000017` can't match by
    # coincidence inside a longer digit run (an account number,
    # amount, or MICR field) and override the real structured read.
    # Values carrying punctuation (e.g. the `21,715.00` amount form)
    # keep the original substring tiers.
    strict_numeric = bool(
        numeric and norm_dom and norm_dom.isdigit() and len(dom_digits) >= 4
    )

    # 1. Exact normalized substring.
    if (
        norm_dom
        and norm_dom in norm_ocr
        and (
            not strict_numeric
            or _digit_run_aligned_present(dom_digits, ocr_text)
        )
    ):
        return {
            "dom_value": dom_value,
            "present": True,
            "match_kind": "exact",
            "similarity": 1.0,
            "note": None,
        }

    # 2. Digit-only substring — for numeric fields (cheque no,
    #    account no, amount) where OCR ate the punctuation.
    if numeric and strict_numeric:
        # Boundary-respecting standalone match, with an OCR
        # letter↔digit-tolerant retry on the rewritten text.
        if _digit_run_aligned_present(dom_digits, ocr_text):
            return {
                "dom_value": dom_value,
                "present": True,
                "match_kind": "digits",
                "similarity": 1.0,
                "note": None,
            }
        # 2b. OCR-tolerant standalone match — rewrite letter↔digit
        #     confusions (i/l/I/L→1, L→4, O/o→0, S→5, B→8/6, …)
        #     inside digit-bearing tokens, preserving boundaries,
        #     then retry the standalone search. Production
        #     motivator: cheque number `143144` came through OCR
        #     as `143iL4`; with L→4 the token rewrites to `143144`
        #     and matches as a standalone number.
        for variant_text in _ocr_letter_digit_text_variants(ocr_text):
            if _digit_run_aligned_present(dom_digits, variant_text):
                return {
                    "dom_value": dom_value,
                    "present": True,
                    "match_kind": "digits_ocr_tolerant",
                    "similarity": 1.0,
                    "note": (
                        "Matched after substituting OCR letter↔digit "
                        "confusions (e.g. i/l/L→1, L→4, O→0, B→8/6) "
                        "inside tokens that contained digits."
                    ),
                }
    elif numeric:
        # Non-pure-digit numeric (amount with commas/decimal):
        # keep the original collapse-and-substring tiers.
        ocr_digits = _digits_only(ocr_text)
        if len(dom_digits) >= 4 and dom_digits in ocr_digits:
            return {
                "dom_value": dom_value,
                "present": True,
                "match_kind": "digits",
                "similarity": 1.0,
                "note": None,
            }
        if len(dom_digits) >= 4:
            for variant_digits in _ocr_letter_digit_variants(ocr_text):
                if (
                    variant_digits
                    and dom_digits in variant_digits
                    and dom_digits not in ocr_digits
                ):
                    return {
                        "dom_value": dom_value,
                        "present": True,
                        "match_kind": "digits_ocr_tolerant",
                        "similarity": 1.0,
                        "note": (
                            "Matched after substituting OCR letter↔digit "
                            "confusions (e.g. i/l/L→1, L→4, O→0, B→8/6) "
                            "inside tokens that contained digits."
                        ),
                    }

    # 3. Fuzzy windowed substring — for textual fields where OCR
    #    swapped a letter or two (e.g. JAYSHIVSAKTHI vs JAY SHIVSAKTI).
    #    NOT used to flip a strict-numeric value to present: a
    #    near-miss number is a DIFFERENT number, not a noisy read of
    #    the same one. We still report the similarity so the caller's
    #    WARN tier and the UI keep a signal.
    sim = _fuzzy_substring(norm_dom, norm_ocr)
    if sim >= _PRESENCE_THRESHOLD and not strict_numeric:
        return {
            "dom_value": dom_value,
            "present": True,
            "match_kind": "fuzzy",
            "similarity": round(sim, 3),
            "note": None,
        }

    return {
        "dom_value": dom_value,
        "present": False,
        "match_kind": None,
        "similarity": round(sim, 3),
        "note": (
            "value not found as a standalone number in OCR text"
            if strict_numeric
            else "value not found in OCR text"
        ),
    }


# Canonical → list of DOM aliases. Maps the field names the
# validation API exposes (lowercase snake_case) to every DOM-key
# spelling we've seen the CTS UAT panel emit. The first alias that
# carries a non-empty value wins.
_PRESENCE_DOM_KEYS: dict[str, tuple[str, ...]] = {
    "beneficiary": ("Beneficiary", "Beneficiary 1"),
    "beneficiary_2": ("Beneficiary 2",),
    "beneficiary_3": ("Beneficiary 3",),
    "account_no": ("Account No", "Account No.", "A/C No", "A/C No.", "A/c No"),
    "cheque_no": ("Cheque No", "Cheque No.", "Cheque Number"),
    "amount": ("Amount",),
    "city": ("City",),
    "bank": ("Bank",),
    "branch": ("Branch",),
    "tc": ("TC",),
}

# Which fields are numeric (so we can also try a digit-only
# substring match for them).
_PRESENCE_NUMERIC: frozenset[str] = frozenset({
    "account_no", "cheque_no", "amount", "city", "bank", "branch", "tc",
})

# Which side of the cheque each field is most likely to be printed
# on. We search that side's OCR text FIRST and fall back to the
# combined corpus only when not found there — this keeps the match
# specific (e.g. account_no found on back, not just the MICR row
# happening to repeat on the front).
_PRESENCE_PRIMARY_SIDE: dict[str, Literal["front", "back", "any"]] = {
    "beneficiary": "front",
    "beneficiary_2": "front",
    "beneficiary_3": "front",
    "amount": "front",
    "cheque_no": "any",
    "account_no": "back",
    "city": "any",
    "bank": "any",
    "branch": "any",
    "tc": "any",
}


def validate_dom_presence(
    *,
    dom: dict[str, Any] | None,
    front_text: str | None,
    back_text: str | None,
) -> dict[str, Any]:
    """For each DOM field the bank's panel exposes, decide whether
    its value appears on the cheque image OCR text and return a
    per-field {dom_value, present, match_kind, similarity, note}
    dict. The UI renders this directly as ✓ / ✗ badges next to the
    panel values.

    `match_kind` will be one of:
      - "exact"  — normalized substring match on the relevant side.
      - "digits" — numeric-only substring match (cheque no, etc.).
      - "fuzzy"  — windowed SequenceMatcher ≥ 0.85.
      - None     — not found (value absent or below all thresholds).
    """
    dom = dom or {}
    front = front_text or ""
    back = back_text or ""
    combined = (front + "\n" + back).strip()

    fields: dict[str, dict[str, Any]] = {}
    for canonical, aliases in _PRESENCE_DOM_KEYS.items():
        dom_value: str | None = None
        for k in aliases:
            v = dom.get(k)
            if v:
                dom_value = str(v).strip()
                break
        # Pick the primary search corpus for this field.
        primary_side = _PRESENCE_PRIMARY_SIDE.get(canonical, "any")
        if primary_side == "front":
            primary_text = front
        elif primary_side == "back":
            primary_text = back
        else:
            primary_text = combined

        result = _check_presence(
            dom_value, primary_text,
            numeric=canonical in _PRESENCE_NUMERIC,
        )
        # If the primary side didn't find it, retry against the
        # combined corpus — handles the case where the bank's panel
        # repeats a value on both sides (e.g. account no printed on
        # the face too).
        if not result["present"] and primary_side != "any" and dom_value:
            fallback = _check_presence(
                dom_value, combined,
                numeric=canonical in _PRESENCE_NUMERIC,
            )
            if fallback["present"]:
                fallback["note"] = (
                    f"matched on combined text (primary side: {primary_side})"
                )
                result = fallback

        fields[canonical] = result

    attempted = sum(1 for f in fields.values() if f["dom_value"])
    matched = sum(1 for f in fields.values() if f["present"] and f["dom_value"])
    return {
        "fields": fields,
        "matched": matched,
        "total": attempted,
    }


def compare_dom_vs_ocr(
    *,
    dom: dict[str, Any] | None,
    front: dict[str, Any] | None,
    back: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the per-cheque validation report the UI renders as ✓/⚠
    badges. We compare the four headline fields:

      - beneficiary: DOM vs front OCR
      - cheque_no:   DOM vs front OCR
      - amount:      DOM vs front OCR
      - account_no:  DOM vs back  OCR (falls back to front if back
                     OCR didn't read it — some banks print the
                     account number on the face too)
    """
    dom = dom or {}
    front = front or {}
    back = back or {}

    fields: dict[str, dict[str, Any]] = {}
    front_ocr = front if isinstance(front, dict) else {}
    back_ocr = back if isinstance(back, dict) else {}

    fields["beneficiary"] = _compare_field(
        "beneficiary",
        _pick_dom_value(dom, "beneficiary"),
        front_ocr.get("beneficiary"),
    )
    fields["cheque_no"] = _compare_field(
        "cheque_no",
        _pick_dom_value(dom, "cheque_no"),
        front_ocr.get("cheque_no"),
    )
    fields["amount"] = _compare_field(
        "amount",
        _pick_dom_value(dom, "amount"),
        front_ocr.get("amount"),
    )
    fields["account_no"] = _compare_field(
        "account_no",
        _pick_dom_value(dom, "account_no"),
        # Prefer the back-side OCR for the account number (it's
        # printed there), but accept the front-side one as a
        # fallback — some cheques carry the A/C on both faces.
        back_ocr.get("account_no") or front_ocr.get("account_no"),
    )

    # Headline counts: how many of the fields we attempted to verify
    # actually agreed between the two sources. We count a field as
    # "attempted" only when BOTH sources supplied a value — fields
    # where one side is blank skew the rate unhelpfully.
    matched = sum(
        1 for f in fields.values()
        if f["match"] and f["dom_value"] and f["ocr_value"]
    )
    total = sum(
        1 for f in fields.values()
        if f["dom_value"] and f["ocr_value"]
    )
    return {
        "fields": fields,
        "matched": matched,
        "total": total,
    }
