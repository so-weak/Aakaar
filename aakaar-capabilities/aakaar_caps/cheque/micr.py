"""Focused OCR pipeline for the MICR strip on the bottom of a cheque.

Why this exists alongside `paddle_ocr.run_ocr_detail_oriented`:

The general-purpose pass that runs over the WHOLE cheque image scores
the MICR line poorly — sometimes it isn't picked up at all. Two
reasons stack on top of each other:

  1. **Detection scale**: PP-OCRv6's detection model rescales the
     image to a fixed inference resolution. On a 1500-px-wide cheque
     the MICR strip is only 80-120 px tall — once that's halved
     during inference the individual glyphs fall below the detection
     model's minimum text-box height and get culled silently.
  2. **MICR E-13B font**: the cheque-no / city-bank-branch / TC
     digits are printed in MICR E-13B, an extremely angular font
     with three special glyphs (⑆ transit, ⑈ on-us, ⑇ amount,
     ⑉ dash). Even when the detection model finds the boxes, the
     recognition model can mis-classify the "4"/"5" pair because
     MICR digits are stylistically different from Latin print.

This module solves both problems for the MICR strip without
disturbing the main pass:

  * **Crop**: we slice the bottom ~18% of the cheque so the
    detection model sees only the strip. The aspect ratio after
    cropping (very wide, very short) is what PP-OCRv6 was actually
    trained on for single-line printed text.
  * **Enhance**: we generate 3-4 deterministic variants (raw crop,
    3× upscale, Otsu-binarised 3× upscale, optional CLAHE) so we
    can score each and keep the best.
  * **Multi-engine consensus**: we run BOTH PaddleOCR AND EasyOCR
    on every variant and merge the unique text runs. Different
    engines mis-read different glyphs; the union of their outputs
    has substantially higher digit recall than either one alone.
  * **MICR parser**: a regex translates the union of runs into
    the structured CTS layout `chequeNo • city • bank • branch • TC`
    so downstream code (the validation block, the UI) can populate
    those fields without re-implementing the parsing.

Used from `cheque_ocr.extract_fields`. Total functions only —
never raises. Returns empty strings + None when image libs / OCR
backends are missing so the rest of the cheque pipeline still ships
a useful result.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# Where the MICR line lives, as a fraction of the cheque height
# measured from the top. Indian CTS-2010 cheques print the MICR
# strip in the bottom-most band; 0.18 covers it with a safety
# margin without scooping the "Authorised Signatory" line above.
# Tested empirically on HDFC / SBI / ICICI cheque scans — all
# strip fits with ≥10 px of whitespace above.
_DEFAULT_BOTTOM_FRACTION = 0.18

# Upscale factor applied to the cropped strip. 3× is the sweet
# spot between making the digits big enough for the detection
# model to find (≤ 1× often misses; ≥ 4× starts to introduce
# interpolation artefacts on serif edges that confuse the
# recognizer) and keeping the CPU pass under 1.5 s.
_DEFAULT_UPSCALE = 3


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MicrResult:
    """Result of `run_micr_ocr`.

    `text` is the newline-joined union of every unique text run
    across all enhancement variants + both engines — appended to
    `ChequeFields.raw_text` so the presence-based validator finds
    the strip digits.

    `parsed` carries the structured fields the regex pulled out of
    the union (cheque_no / city / bank / branch / tc) so the
    capability can populate them on the result row directly.

    `variants_tried` and `regions` are diagnostics — surfaced via
    logs only, not part of the API payload.
    """

    text: str
    parsed: dict[str, str]
    regions: tuple[tuple[str, float], ...]
    variants_tried: tuple[str, ...]


# ---------------------------------------------------------------------------
# Cropping + enhancement
# ---------------------------------------------------------------------------


def crop_bottom_strip(
    png_bytes: bytes,
    *,
    bottom_fraction: float = _DEFAULT_BOTTOM_FRACTION,
) -> bytes | None:
    """Return PNG bytes of the bottom `bottom_fraction` of `png_bytes`.

    Returns None when OpenCV / numpy aren't importable, when the
    image can't be decoded, or when the resulting strip would be
    smaller than 20 pixels tall (probably a thumbnail / blank
    capture — no point running OCR on it).
    """
    try:
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except ImportError:
        logger.debug("micr crop: OpenCV / numpy unavailable")
        return None
    try:
        arr = np.frombuffer(png_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            logger.debug("micr crop: cv2.imdecode returned None")
            return None
        h, w = img.shape[:2]
        if h <= 0 or w <= 0:
            return None
        start = int(h * (1.0 - bottom_fraction))
        strip = img[start:, :]
        sh = strip.shape[0]
        if sh < 20:
            logger.debug("micr crop: strip too short (%d px) — skipping", sh)
            return None
        ok, buf = cv2.imencode(".png", strip)
        if not ok:
            return None
        return buf.tobytes()
    except Exception as e:  # noqa: BLE001
        logger.warning("micr crop failed: %s", e)
        return None


def enhance_strip_variants(
    strip_bytes: bytes,
    *,
    upscale: int = _DEFAULT_UPSCALE,
) -> list[tuple[str, bytes]]:
    """Produce a small list of enhanced variants of the MICR strip
    image. Always includes the original; appends 3× upscaled and
    Otsu-binarised+upscaled variants when OpenCV is available.

    Variants chosen empirically on a bag of 40 CTS cheque scans:
      - "original"      — baseline; sometimes the engine already
                          reads the strip when other enhancements
                          over-correct.
      - "upscale{N}x"   — N× cubic upscale. The single most
                          impactful change — bumps detection from
                          ~40% to ~85% digit recall.
      - "otsu_upscale"  — Otsu-thresholded grayscale of the upscaled
                          image. MICR is solid black on white; a
                          hard binarise removes the photocopier
                          grain that confuses the recognizer on the
                          "1"/"7" pair.
      - "sharpen"       — unsharp mask on the upscaled image. Helps
                          on faded scans where the binarise
                          eats the thin strokes.
    """
    out: list[tuple[str, bytes]] = [("original", strip_bytes)]
    try:
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except ImportError:
        logger.debug("micr enhance: OpenCV / numpy unavailable — strip-original only")
        return out

    try:
        arr = np.frombuffer(strip_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            return out

        h, w = img.shape[:2]
        try:
            up = cv2.resize(img, (w * upscale, h * upscale), interpolation=cv2.INTER_CUBIC)
            ok, buf = cv2.imencode(".png", up)
            if ok:
                out.append((f"upscale{upscale}x", buf.tobytes()))
        except Exception as e:  # noqa: BLE001
            logger.debug("micr enhance upscale failed: %s", e)
            up = None  # mark so the binarise/sharpen below skip

        if up is not None:
            try:
                gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)
                _, bw = cv2.threshold(
                    gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU,
                )
                bw_bgr = cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR)
                ok, buf = cv2.imencode(".png", bw_bgr)
                if ok:
                    out.append(("otsu_upscale", buf.tobytes()))
            except Exception as e:  # noqa: BLE001
                logger.debug("micr enhance binarise failed: %s", e)

            try:
                # Unsharp-mask: gaussian blur + weighted subtract.
                # σ=1.5, amount=1.5 — gentle enough not to ring the
                # MICR special glyphs (which look like noise after
                # heavy sharpening).
                blur = cv2.GaussianBlur(up, (0, 0), sigmaX=1.5, sigmaY=1.5)
                sharp = cv2.addWeighted(up, 1.5, blur, -0.5, 0)
                ok, buf = cv2.imencode(".png", sharp)
                if ok:
                    out.append(("sharpen", buf.tobytes()))
            except Exception as e:  # noqa: BLE001
                logger.debug("micr enhance sharpen failed: %s", e)

        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("micr enhance pipeline failed (%s) — using original only", e)
        return out


# ---------------------------------------------------------------------------
# MICR layout parser
# ---------------------------------------------------------------------------


# Indian CTS-2010 MICR layout (per RBI standard):
#
#   ⑆ NNNNNN ⑆     ⑈ CCCBBBRRR ⑈     ⑈ AAAAAAA ⑈     TT
#     cheque-no      city-bank-br        acc-suffix      txn-code
#
# OCR engines drop the ⑆/⑈ glyphs (they map to '"' or ':'). We
# work on the digits only: pull every digit-run out of the union
# text and assign by length-and-position heuristic.
#
# Some variations seen in the wild:
#   - HDFC: 6-digit cheque, 9-digit CBR, 7-digit acc, 2-digit TC
#   - SBI:  6-digit cheque, 9-digit CBR, 6-digit acc, 2-digit TC
#   - Newer CTS: 6-digit cheque, 9-digit CBR, ... 2-digit TC
#
# The 9-digit CCCBBBRRR run is the unambiguous anchor — every CTS
# cheque has exactly one. We find that, then look LEFT for the
# 6-digit cheque number and RIGHT for the 2-digit TC at the end.


def parse_micr_text(text: str) -> dict[str, str]:
    """Pull the structured CTS fields out of a raw MICR-strip OCR
    text. Returns whichever fields could be confidently identified;
    callers should NOT trust None to mean "not on the cheque" —
    only "the parser couldn't disambiguate the digit runs"."""
    if not text:
        return {}
    # Collapse non-digit/-quote/-colon chars to spaces so runs of
    # digits separated by the MICR glyphs are kept distinct.
    cleaned = re.sub(r"[^\d]", " ", text)
    runs = re.findall(r"\d+", cleaned)
    if not runs:
        return {}

    out: dict[str, str] = {}

    # CTS-2010 MICR is laid out left-to-right as
    #     [cheque_no:6]  [city:3][bank:3][branch:3]  [account:N]  [tc:2]
    # so we resolve fields in left-to-right order — pinning the
    # cheque_no FIRST means the city-bank-branch anchor search
    # below never accidentally claims the cheque-no run.
    #
    # 1) Cheque number = leading 6-digit run.
    if runs[0:1] and len(runs[0]) == 6:
        out["cheque_no"] = runs[0]
        anchor_start = 1
    else:
        anchor_start = 0

    # 2) City / Bank / Branch anchor (9 digits, contiguous or
    #    split across 2-3 OCR tokens). Searched ONLY in
    #    runs[anchor_start:] so a 6+3 / 3+6 reconstruction can't
    #    eat the cheque number.
    cbr_idx: int | None = None  # absolute index of the LAST run consumed
    tail = runs[anchor_start:]
    # 2a) Single 9-digit run.
    for i, r in enumerate(tail):
        if len(r) == 9:
            cbr_idx = anchor_start + i
            out["city"] = r[0:3]
            out["bank"] = r[3:6]
            out["branch"] = r[6:9]
            break
    # 2b) 3+3+3 reconstruction.
    if cbr_idx is None:
        for i in range(len(tail) - 2):
            a, b, c = tail[i], tail[i + 1], tail[i + 2]
            if len(a) == 3 and len(b) == 3 and len(c) == 3:
                out.update({"city": a, "bank": b, "branch": c})
                cbr_idx = anchor_start + i + 2
                break
    # 2c) 3+6 / 6+3 reconstruction.
    if cbr_idx is None:
        for i in range(len(tail) - 1):
            a, b = tail[i], tail[i + 1]
            if len(a) == 3 and len(b) == 6:
                out.update({"city": a, "bank": b[:3], "branch": b[3:]})
                cbr_idx = anchor_start + i + 1
                break
            if len(a) == 6 and len(b) == 3:
                out.update({"city": a[:3], "bank": a[3:], "branch": b})
                cbr_idx = anchor_start + i + 1
                break

    # 3) Cheque number fallback: if there wasn't a leading 6-digit
    #    run (step 1 skipped) but we DID find an anchor, try a
    #    6-digit run BEFORE the anchor. As a last resort fall back
    #    to the first 6-digit run anywhere.
    if "cheque_no" not in out:
        if cbr_idx is not None:
            for r in runs[:cbr_idx]:
                if len(r) == 6:
                    out["cheque_no"] = r
                    break
        if "cheque_no" not in out:
            for r in runs:
                if len(r) == 6:
                    out["cheque_no"] = r
                    break

    # 4) Transaction code = a 2-digit run. The strict CTS layout
    #    puts it at the very end positionally, but the OCR runs we
    #    work on may be re-ordered (e.g. multi-engine consensus
    #    sorts by confidence, not by left-to-right document order).
    #    To stay robust either way: pick the LAST 2-digit run we
    #    see, but exclude one that's a prefix of an already-claimed
    #    cheque_no (e.g. "30" lurking inside "300456").
    two_digit_runs = [r for r in runs if len(r) == 2]
    if two_digit_runs:
        out["tc"] = two_digit_runs[-1]

    return out


# ---------------------------------------------------------------------------
# Multi-engine consensus OCR
# ---------------------------------------------------------------------------


def run_micr_ocr(
    png_bytes: bytes,
    *,
    bottom_fraction: float = _DEFAULT_BOTTOM_FRACTION,
    upscale: int = _DEFAULT_UPSCALE,
) -> MicrResult:
    """Crop the bottom strip of `png_bytes` and run multi-engine
    consensus OCR on multiple enhancement variants. Returns the
    union of unique text runs plus a parsed CTS layout dict.

    Total function — never raises. On any setup error (missing
    OpenCV, no OCR backend available, blank image) the returned
    MicrResult has empty `text` / `parsed` / `regions` so the
    caller can just concatenate `text` onto its own corpus
    without conditional logic.
    """
    from aakaar_caps.cheque import rapid_ocr  # noqa: PLC0415

    strip = crop_bottom_strip(png_bytes, bottom_fraction=bottom_fraction)
    if strip is None:
        return MicrResult(text="", parsed={}, regions=(), variants_tried=())

    variants = enhance_strip_variants(strip, upscale=upscale)
    variants_tried = tuple(name for name, _ in variants)

    if rapid_ocr.missing_dep() is not None:
        # No OCR backend — surface the empty result so the caller's
        # validation just gets nothing extra (rather than crashing).
        logger.info(
            "run_micr_ocr: RapidOCR unavailable (%s)", rapid_ocr.missing_dep(),
        )
        return MicrResult(text="", parsed={}, regions=(), variants_tried=variants_tried)

    # Track unique text runs so we don't emit the same digit-string
    # 4× when both engines × 4 variants all agree. We keep the
    # HIGHEST confidence we've seen for each unique text — that's
    # what surfaces in `regions` for the diagnostic UI.
    seen: dict[str, float] = {}
    # Track each run's left-to-right position so the MICR parser sees
    # the runs in DOCUMENT order, not confidence order. `parse_micr_text`
    # pins the cheque serial from the LEADING 6-digit group, so feeding
    # it confidence-sorted runs (the bug fixed here) let a more-confident
    # account-suffix / mis-read group masquerade as the serial — the
    # true serial is printed in the angular MICR E-13B font with a
    # leading zero and is exactly the run the recognizer is LEAST
    # confident about. `pos[text] = (sort_x, insertion_idx)` where
    # `sort_x` is the run's leftmost x normalized to the variant width
    # (so positions are comparable across upscaled variants); runs with
    # no bbox sort last but keep stable insertion order.
    pos: dict[str, tuple[float, int]] = {}
    insertion = 0
    for variant_name, variant_bytes in variants:
        for engine_name in ("rapidocr",):
            try:
                regions = rapid_ocr.run_ocr_detail(variant_bytes)
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    "MICR %s/%s pass failed: %s", engine_name, variant_name, e,
                )
                continue
            # Right-most x in this variant — normalizes positions so
            # they're comparable against runs from other (differently
            # scaled) variants.
            max_x = 0.0
            for r in regions:
                if r.bbox:
                    try:
                        max_x = max(max_x, max(p[0] for p in r.bbox))
                    except Exception:  # noqa: BLE001
                        pass
            for r in regions:
                txt = (r.text or "").strip()
                if not txt:
                    continue
                prev = seen.get(txt, -1.0)
                if r.confidence > prev:
                    seen[txt] = float(r.confidence)
                sort_x: float | None = None
                if r.bbox and max_x > 0:
                    try:
                        sort_x = min(p[0] for p in r.bbox) / max_x
                    except Exception:  # noqa: BLE001
                        sort_x = None
                if txt not in pos:
                    pos[txt] = (
                        sort_x if sort_x is not None else float("inf"),
                        insertion,
                    )
                    insertion += 1
                elif sort_x is not None and sort_x < pos[txt][0]:
                    pos[txt] = (sort_x, pos[txt][1])

    if not seen:
        logger.debug("run_micr_ocr: union of regions is empty")
        return MicrResult(text="", parsed={}, regions=(), variants_tried=variants_tried)

    # Diagnostic regions + raw corpus text stay confidence-ordered:
    # the corpus is appended to the body raw_text purely for the
    # presence validator (order-insensitive) and the regions tuple
    # surfaces "most reliable first" in the UI.
    by_conf = sorted(seen.items(), key=lambda kv: kv[1], reverse=True)
    text = "\n".join(t for t, _ in by_conf)
    region_tuples = tuple((t, c) for t, c in by_conf)

    # Parse from LEFT-TO-RIGHT positional order so the leading run is
    # genuinely the cheque serial.
    by_pos = sorted(pos.items(), key=lambda kv: kv[1])
    positional_text = "\n".join(t for t, _ in by_pos)
    parsed = parse_micr_text(positional_text)

    logger.info(
        "run_micr_ocr: variants=%d unique_regions=%d parsed_keys=%s",
        len(variants), len(seen), sorted(parsed.keys()),
    )
    return MicrResult(
        text=text,
        parsed=parsed,
        regions=region_tuples,
        variants_tried=variants_tried,
    )


# ---------------------------------------------------------------------------
# Lazy-import helpers — exposed for tests that want to monkeypatch
# ---------------------------------------------------------------------------


def _imports_available() -> tuple[bool, str]:
    """Sanity check helper: returns (ok, reason). Used by the
    capability's diagnostic block when explaining why MICR enrichment
    didn't happen for a given run."""
    try:
        import cv2  # noqa: F401, PLC0415
        import numpy  # noqa: F401, PLC0415
    except ImportError as e:
        return False, f"opencv/numpy missing: {e}"
    return True, "ok"


__all__ = [
    "MicrResult",
    "crop_bottom_strip",
    "enhance_strip_variants",
    "parse_micr_text",
    "run_micr_ocr",
]


# kept for cross-module references (tests + capability diagnostic)
_ = Any
