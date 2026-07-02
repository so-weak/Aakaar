"""RapidOCR PP-OCR — the single, cross-platform cheque OCR engine.

Why this is the ONLY OCR engine in the cheque pipeline:

  RapidOCR runs Baidu's PP-OCR detection + recognition models through
  ``onnxruntime`` — pure ONNX, CPU-only, no PyTorch, no PaddlePaddle,
  no platform-specific framework. The models ship INSIDE the wheel
  (``site-packages/rapidocr/models/``) so there is no first-run model
  download and it works fully offline behind a corporate proxy.

  It replaces the previous multi-engine stack (GOT-OCR2, Apple Vision,
  PaddleOCR/EasyOCR, docTR, TrOCR, the local VLM) which was slow,
  heavy, and — in Apple Vision's case — macOS-only. RapidOCR reads a
  full cheque face in ~0.2-0.6 s on CPU on Win/Linux/macOS alike.

When this DOESN'T run:

  * ``rapidocr`` / ``onnxruntime`` not installed — ``missing_dep()``
    returns the install hint and the cheque pipeline degrades to a
    clean "OCR unavailable" result instead of crashing.
  * ``AAKAAR_RAPIDOCR_DISABLE=true`` — operator kill-switch.

Public API (mirrors the old ``paddle_ocr`` / ``apple_vision_ocr``
surface so callers don't special-case the engine):

  * ``run_ocr_text(png_bytes) -> (str, float)``  — joined text + avg conf
  * ``run_ocr_detail(png_bytes) -> list[OcrRegion]`` — per-region detail
  * ``run_ocr_on_region(png_bytes, bbox) -> RegionResult`` — crop + read
  * ``missing_dep() -> str | None`` — explain unavailability (eager)
  * ``cached_missing_dep() -> str | None`` — passive variant
  * ``reset_for_tests()`` — clear the cached engine singleton
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


def _quiet_rapidocr_logger() -> None:
    """RapidOCR attaches its own coloured StreamHandler and logs an INFO
    line per model file on every engine init — far too chatty for a
    server log. Detach its handlers, stop propagation, and pin it to
    ERROR so only genuine failures surface."""
    rl = logging.getLogger("RapidOCR")
    rl.setLevel(logging.ERROR)
    rl.propagate = False
    for h in list(rl.handlers):
        rl.removeHandler(h)
    rl.addHandler(logging.NullHandler())


_quiet_rapidocr_logger()


# ---------------------------------------------------------------------------
# Public types — shape-compatible with the engines this one replaced
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OcrRegion:
    """One detected text region.

    ``bbox`` is a list of 4 ``(x, y)`` points in pixel coordinates with
    a TOP-LEFT origin (matching the old ``paddle_ocr.OcrRegion`` /
    ``apple_vision_ocr.OcrRegion`` so downstream parsers — MICR
    positional sort, consensus bbox defaults — keep working).
    """

    text: str
    confidence: float
    bbox: list[list[float]]

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "confidence": self.confidence, "bbox": self.bbox}


@dataclass(frozen=True, slots=True)
class RegionResult:
    """Output of ``run_ocr_on_region`` — shape-compatible with the old
    ``paddle_ocr.RegionResult`` so cheque_ocr's focused passes can call
    it without changes."""

    text: str
    confidence: float
    region_count: int
    used_variant: str
    missing_dep: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "region_count": self.region_count,
            "used_variant": self.used_variant,
            "missing_dep": self.missing_dep,
        }


# ---------------------------------------------------------------------------
# Module state — engine loaded lazily, cached, thread-safe
# ---------------------------------------------------------------------------

_load_lock = threading.Lock()
_load_attempted: bool = False
_load_failure_reason: str | None = None
_engine_obj: Any = None

# onnxruntime InferenceSession.run is thread-safe, but the RapidOCR
# Python wrapper threads mutable state through pre/post-processing.
# Serialise inference behind a process-wide lock — each call is only
# ~0.2-0.6s so the queueing cost is dominated by the inference cost
# (this mirrors what apple_vision_ocr did and never showed a
# measurable wall-clock regression vs. true parallelism).
_inference_lock = threading.Lock()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _load_engine() -> bool:
    """Lazily construct the RapidOCR engine. Returns True when ready,
    False when the package/install can't support it; in the False case
    ``_load_failure_reason`` carries the operator-facing diagnostic."""
    global _load_attempted, _load_failure_reason, _engine_obj

    if _load_attempted and (
        _engine_obj is not None or _load_failure_reason is not None
    ):
        return _engine_obj is not None

    with _load_lock:
        if _load_attempted and (
            _engine_obj is not None or _load_failure_reason is not None
        ):
            return _engine_obj is not None
        _load_attempted = True
        _load_failure_reason = None

        if _env_bool("AAKAAR_RAPIDOCR_DISABLE", default=False):
            _load_failure_reason = (
                "RapidOCR disabled via AAKAAR_RAPIDOCR_DISABLE=true"
            )
            logger.info("rapid_ocr: %s", _load_failure_reason)
            return False

        try:
            # Unified package (>=2.0) exposes `from rapidocr import
            # RapidOCR`; the legacy ONNX-only package exposes it under
            # `rapidocr_onnxruntime`. Support both so the engine works
            # regardless of which wheel the host has.
            try:
                from rapidocr import RapidOCR  # noqa: PLC0415
            except ImportError:
                from rapidocr_onnxruntime import RapidOCR  # noqa: PLC0415
        except ImportError as e:
            _load_failure_reason = (
                f"rapidocr not installed: {e}. Install with "
                f"`pip install -e \".[cheque-ocr]\"` (pulls rapidocr + "
                f"onnxruntime; models ship in the wheel, no download)."
            )
            logger.info("rapid_ocr: %s", _load_failure_reason)
            return False
        except BaseException as e:  # noqa: BLE001
            _load_failure_reason = (
                f"rapidocr import failed: {type(e).__name__}: {e}"
            )
            logger.warning("rapid_ocr: %s", _load_failure_reason)
            return False

        try:
            _quiet_rapidocr_logger()  # re-assert: RapidOCR re-adds handlers
            _engine_obj = RapidOCR()
            _quiet_rapidocr_logger()
        except BaseException as e:  # noqa: BLE001
            _load_failure_reason = (
                f"RapidOCR engine init failed: {type(e).__name__}: {e}"
            )
            logger.warning("rapid_ocr: %s", _load_failure_reason)
            _engine_obj = None
            return False

        logger.info("rapid_ocr: RapidOCR engine ready (PP-OCR via onnxruntime)")
        return True


def missing_dep() -> str | None:
    """Reason RapidOCR can't run, or None when ready. Eager — triggers
    the one-time engine load on first call (mirrors the old
    ``paddle_ocr.missing_dep`` / ``apple_vision_ocr.missing_dep``)."""
    _load_engine()
    if _engine_obj is not None:
        return None
    return _load_failure_reason


def cached_missing_dep() -> str | None:
    """Passive variant — returns the cached reason WITHOUT triggering a
    new load attempt. Use from hot paths to skip dispatch without
    paying the load cost again."""
    if not _load_attempted:
        return None
    if _engine_obj is not None:
        return None
    return _load_failure_reason


def reset_for_tests() -> None:
    """Tests only — clear the cached engine so a fresh load attempt can
    be exercised."""
    global _load_attempted, _load_failure_reason, _engine_obj
    with _load_lock:
        _load_attempted = False
        _load_failure_reason = None
        _engine_obj = None


# ---------------------------------------------------------------------------
# Core inference
# ---------------------------------------------------------------------------


def _decode_bgr(png_bytes: bytes) -> Any | None:
    """Decode image bytes to a BGR ndarray. Returns None on failure."""
    if not png_bytes:
        return None
    try:
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except ImportError as e:
        logger.debug("rapid_ocr: opencv/numpy missing: %s", e)
        return None
    try:
        arr = np.frombuffer(png_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            return None
        return img
    except BaseException as e:  # noqa: BLE001
        logger.debug("rapid_ocr: image decode failed: %s", e)
        return None


def _box_to_points(box: Any) -> list[list[float]]:
    """Normalise one RapidOCR box (np.ndarray of 4 points, or a nested
    list) into ``list[[x, y], ...]`` of plain floats."""
    try:
        return [[float(p[0]), float(p[1])] for p in box]
    except BaseException:  # noqa: BLE001
        return []


def _run(img_bgr: Any) -> tuple[list[OcrRegion], float]:
    """Run RapidOCR on a decoded BGR ndarray. Returns (regions, avg
    confidence). Never raises — errors log and return ([], 0.0) so
    callers can degrade cleanly."""
    if not _load_engine() or _engine_obj is None:
        return ([], 0.0)
    if img_bgr is None:
        return ([], 0.0)

    try:
        with _inference_lock:
            result = _engine_obj(img_bgr)
    except BaseException as e:  # noqa: BLE001
        logger.warning("rapid_ocr: inference raised: %s", e)
        return ([], 0.0)

    if result is None:
        return ([], 0.0)

    # RapidOCR >=2.0 returns a RapidOCROutput with .txts/.boxes/.scores.
    # Legacy (<2.0) returns (list[[box, text, score], ...], elapse).
    txts = getattr(result, "txts", None)
    if txts is not None:
        boxes = getattr(result, "boxes", None)
        scores = getattr(result, "scores", None)
        return _from_output(txts, boxes, scores)
    return _from_legacy(result)


def _from_output(txts: Any, boxes: Any, scores: Any) -> tuple[list[OcrRegion], float]:
    regions: list[OcrRegion] = []
    confs: list[float] = []
    boxes_list = list(boxes) if boxes is not None else []
    scores_list = list(scores) if scores is not None else []
    for i, txt in enumerate(list(txts or [])):
        text = str(txt or "").strip()
        if not text:
            continue
        conf = float(scores_list[i]) if i < len(scores_list) else 0.0
        bbox = _box_to_points(boxes_list[i]) if i < len(boxes_list) else []
        regions.append(OcrRegion(text=text, confidence=conf, bbox=bbox))
        confs.append(conf)
    avg = sum(confs) / len(confs) if confs else 0.0
    return (regions, avg)


def _from_legacy(result: Any) -> tuple[list[OcrRegion], float]:
    # result is a list of [box, text, score]; sometimes wrapped as
    # (result, elapse) — but _run only passes the first element here.
    rows = result
    if isinstance(result, tuple) and result and isinstance(result[0], list):
        rows = result[0]
    regions: list[OcrRegion] = []
    confs: list[float] = []
    for row in rows or []:
        try:
            box, text, score = row[0], row[1], row[2]
        except BaseException:  # noqa: BLE001
            continue
        text = str(text or "").strip()
        if not text:
            continue
        conf = float(score or 0.0)
        regions.append(
            OcrRegion(text=text, confidence=conf, bbox=_box_to_points(box))
        )
        confs.append(conf)
    avg = sum(confs) / len(confs) if confs else 0.0
    return (regions, avg)


# ---------------------------------------------------------------------------
# Public API — mirrors the old paddle_ocr.run_ocr_* surface
# ---------------------------------------------------------------------------


def run_ocr_text(png_bytes: bytes) -> tuple[str, float]:
    """Run RapidOCR on the full image and return (joined_text, avg_conf).
    Returns ("", 0.0) when the engine isn't loadable or the image can't
    be decoded."""
    regions, avg = _run(_decode_bgr(png_bytes))
    if not regions:
        return ("", 0.0)
    return ("\n".join(r.text for r in regions), avg)


def run_ocr_detail(png_bytes: bytes) -> list[OcrRegion]:
    """Per-region detail mirror of the old ``paddle_ocr.run_ocr_detail``.
    Returns typed OcrRegion objects so callers can inspect bboxes /
    confidences (MICR positional sort relies on the bbox)."""
    regions, _avg = _run(_decode_bgr(png_bytes))
    return regions


def run_ocr_on_region(
    png_bytes: bytes,
    bbox: tuple[float, float, float, float],
    *,
    target_height: int = 0,
    enhance: bool = False,
    binarize: bool = False,
) -> RegionResult:
    """Crop a fractional bbox out of ``png_bytes`` and run RapidOCR on
    it. ``bbox`` is ``(x_frac_left, y_frac_top, x_frac_right,
    y_frac_bottom)`` in [0..1]. Mirrors the old
    ``paddle_ocr.run_paddle_on_region`` so cheque_ocr's focused passes
    keep working. Never raises.

    ``target_height`` (> 0) upscales the crop with cubic interpolation
    so its height ≈ ``target_height`` (capped at 10x) — small
    handwritten boxes (e.g. a ~30px courtesy-amount band) are otherwise
    too low-resolution for the recognizer. ``enhance`` additionally
    applies grayscale → CLAHE → unsharp mask, which lets RapidOCR read
    handwritten cheque amounts that the full-page pass mangles (e.g.
    '47605=00' read as '00=509Lh' on the full page). ``binarize``
    applies a grayscale → Otsu threshold (after ``enhance`` when both
    are set), which sometimes wins on high-contrast ink that the unsharp
    path over-softens — used by the best-of-N amount-words preprocessing
    sweep in ``cheque_ocr``."""
    miss = missing_dep()
    if miss is not None:
        return RegionResult(
            text="", confidence=0.0, region_count=0,
            used_variant="(skipped)", missing_dep=miss,
        )

    try:
        import cv2  # noqa: PLC0415
    except ImportError as e:
        return RegionResult(
            text="", confidence=0.0, region_count=0,
            used_variant="(skipped)", missing_dep=f"opencv missing: {e}",
        )

    img = _decode_bgr(png_bytes)
    if img is None:
        return RegionResult(
            text="", confidence=0.0, region_count=0,
            used_variant="(skipped)", missing_dep="image decode failed",
        )
    try:
        h, w = img.shape[:2]
        x0 = max(0, int(bbox[0] * w))
        y0 = max(0, int(bbox[1] * h))
        x1 = min(w, int(bbox[2] * w))
        y1 = min(h, int(bbox[3] * h))
        if x1 <= x0 or y1 <= y0 or (x1 - x0) < 8 or (y1 - y0) < 8:
            return RegionResult(
                text="", confidence=0.0, region_count=0,
                used_variant="(skipped)",
                missing_dep="crop region too small or invalid bbox",
            )
        crop = img[y0:y1, x0:x1]
        if target_height and crop.shape[0] > 0:
            scale = max(1.0, min(10.0, float(target_height) / crop.shape[0]))
            if scale > 1.0:
                crop = cv2.resize(
                    crop, None, fx=scale, fy=scale,
                    interpolation=cv2.INTER_CUBIC,
                )
        if enhance:
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
            blur = cv2.GaussianBlur(gray, (0, 0), 3)
            gray = cv2.addWeighted(gray, 1.5, blur, -0.5, 0)
            crop = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        if binarize:
            # Otsu auto-thresholds the (possibly enhanced) crop into pure
            # black ink on white paper — robust to the varied contrast
            # across bank cheque stationery without a hand-tuned cutoff.
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            _t, bw = cv2.threshold(
                gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU,
            )
            crop = cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR)
    except BaseException as e:  # noqa: BLE001
        return RegionResult(
            text="", confidence=0.0, region_count=0,
            used_variant="(skipped)",
            missing_dep=f"crop failed: {type(e).__name__}: {e}",
        )

    regions, avg = _run(crop)
    if not regions:
        return RegionResult(
            text="", confidence=0.0, region_count=0,
            used_variant="rapidocr:no_regions", missing_dep=None,
        )
    return RegionResult(
        text="\n".join(r.text for r in regions),
        confidence=avg,
        region_count=len(regions),
        used_variant="rapidocr",
        missing_dep=None,
    )
