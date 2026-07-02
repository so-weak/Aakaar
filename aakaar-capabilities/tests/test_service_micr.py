"""Unit tests for aakar.services.micr — the focused MICR-strip OCR
pipeline that runs alongside the general-purpose cheque OCR pass.

The module has three responsibilities and we test each one
separately so a regression in one doesn't mask another:

  1. **Cropping**: `crop_bottom_strip` cuts the bottom slice of a
     PNG. We feed it a small synthetic image and verify the
     returned bytes decode to the expected sub-image height.

  2. **Enhancement variants**: `enhance_strip_variants` should
     always include the original and, when OpenCV is available,
     add upscale / binarise / sharpen variants without raising.

  3. **MICR parser**: `parse_micr_text` translates an OCR'd MICR
     line into structured CTS fields. This is pure regex over a
     string and is the part that drives the user-visible
     City/Bank/Branch/TC values.

We do NOT spin up a real PaddleOCR / EasyOCR reader here —
`run_micr_ocr` is exercised end-to-end in
`test_service_cheque_ocr.test_micr_strip_text_is_appended_to_raw_text`
by mocking the engine adapters; this keeps the test suite
runnable on machines without the `cheque-ocr` extras installed.
"""

from __future__ import annotations

import io

import pytest


# ---------------------------------------------------------------------------
# Helpers — synthesise tiny PNGs the cropper can operate on without
# requiring opencv to also be importable.
# ---------------------------------------------------------------------------


def _make_png(width: int, height: int) -> bytes:
    """Build a `width × height` solid-white PNG via Pillow. Pillow is
    a hard dep of the project so this always works."""
    from PIL import Image

    img = Image.new("RGB", (width, height), color=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _decode_dimensions(png_bytes: bytes) -> tuple[int, int]:
    from PIL import Image

    img = Image.open(io.BytesIO(png_bytes))
    return img.width, img.height


# ---------------------------------------------------------------------------
# parse_micr_text — pure-regex layout parser
# ---------------------------------------------------------------------------


def test_parse_micr_text_extracts_full_layout() -> None:
    """The canonical CTS-2010 MICR line from the user's reference
    cheque: 6-digit cheque, 9-digit city-bank-branch, account
    suffix, 2-digit TC. The parser should pull every field
    even when the OCR run-up included the special MICR glyphs
    (rendered as quotes/colons by general-purpose OCR engines)."""
    from aakaar_caps.cheque.micr import parse_micr_text

    text = '"378781" 607060202: 010195" 30'
    out = parse_micr_text(text)
    assert out == {
        "cheque_no": "378781",
        "city": "607",
        "bank": "060",
        "branch": "202",
        "tc": "30",
    }


def test_parse_micr_text_reconstructs_split_cbr_anchor() -> None:
    """Some scans split the 9-digit city-bank-branch into 3+3+3
    runs (the recognizer treats wide MICR-glyph gaps as token
    boundaries). The parser must still reconstruct city/bank/branch
    from the 3-3-3 pattern."""
    from aakaar_caps.cheque.micr import parse_micr_text

    text = "378781 607 060 202 010195 30"
    out = parse_micr_text(text)
    assert out["city"] == "607"
    assert out["bank"] == "060"
    assert out["branch"] == "202"
    assert out["cheque_no"] == "378781"
    assert out["tc"] == "30"


def test_parse_micr_text_reconstructs_3_6_split() -> None:
    """3+6 split (city alone, then bank+branch concatenated). Seen
    on a handful of SBI / ICICI scans."""
    from aakaar_caps.cheque.micr import parse_micr_text

    text = "999777 400 002001 8888 42"
    out = parse_micr_text(text)
    assert out["city"] == "400"
    assert out["bank"] == "002"
    assert out["branch"] == "001"
    assert out["cheque_no"] == "999777"
    assert out["tc"] == "42"


def test_parse_micr_text_empty_input_returns_empty_dict() -> None:
    from aakaar_caps.cheque.micr import parse_micr_text

    assert parse_micr_text("") == {}
    assert parse_micr_text("no digits here") == {}


def test_parse_micr_text_missing_cbr_still_returns_cheque_no() -> None:
    """OCR sometimes ate the city-bank-branch run entirely. We
    should still surface the cheque number alone — better one good
    field than zero."""
    from aakaar_caps.cheque.micr import parse_micr_text

    text = "378781   ??????   30"
    out = parse_micr_text(text)
    assert out.get("cheque_no") == "378781"
    assert out.get("tc") == "30"
    # No 9-digit (or 3+3+3 / 3+6 / 6+3) anchor → city/bank/branch absent.
    assert "city" not in out


# ---------------------------------------------------------------------------
# crop_bottom_strip — image slicing
# ---------------------------------------------------------------------------


def test_crop_bottom_strip_returns_bottom_slice_at_default_fraction() -> None:
    """A 1000 × 500 image cropped at the default 18% should yield
    a strip of height round(500 * 0.18) = 90 px."""
    pytest.importorskip("cv2")
    from aakaar_caps.cheque.micr import crop_bottom_strip

    src = _make_png(1000, 500)
    strip = crop_bottom_strip(src)
    assert strip is not None
    w, h = _decode_dimensions(strip)
    assert w == 1000
    assert h == 90  # 500 * 0.18


def test_crop_bottom_strip_custom_fraction() -> None:
    """Operator can dial the crop down (e.g. for cheques where the
    MICR line is unusually tall) without touching the module."""
    pytest.importorskip("cv2")
    from aakaar_caps.cheque.micr import crop_bottom_strip

    src = _make_png(800, 400)
    strip = crop_bottom_strip(src, bottom_fraction=0.10)
    assert strip is not None
    _, h = _decode_dimensions(strip)
    assert h == 40  # 400 * 0.10


def test_crop_bottom_strip_returns_none_for_blank_input() -> None:
    """Garbage bytes → None, no exception. The caller's contract is
    'safe to concatenate / safe to skip'."""
    pytest.importorskip("cv2")
    from aakaar_caps.cheque.micr import crop_bottom_strip

    assert crop_bottom_strip(b"") is None
    assert crop_bottom_strip(b"not-a-png") is None


def test_crop_bottom_strip_returns_none_for_too_small_strip() -> None:
    """A 100 × 50 thumbnail cropped at 18% would give a 9-px strip
    — too short to be a real MICR line. Skip rather than waste an
    OCR pass on it."""
    pytest.importorskip("cv2")
    from aakaar_caps.cheque.micr import crop_bottom_strip

    src = _make_png(100, 50)
    assert crop_bottom_strip(src) is None


# ---------------------------------------------------------------------------
# enhance_strip_variants — preprocessing fan-out
# ---------------------------------------------------------------------------


def test_enhance_strip_variants_always_includes_original() -> None:
    """Even when OpenCV is absent we should get back the original
    bytes — the caller still gets ONE thing to OCR."""
    from aakaar_caps.cheque.micr import enhance_strip_variants

    src = _make_png(400, 80)
    variants = enhance_strip_variants(src)
    assert any(name == "original" for name, _ in variants)
    # And the original-pair's bytes are identical to the input.
    name, blob = variants[0]
    assert name == "original"
    assert blob == src


def test_enhance_strip_variants_emits_upscale_and_binarise_when_cv_present() -> None:
    """Happy path: with OpenCV available, we should see at least
    `original`, `upscale3x`, `otsu_upscale`, and `sharpen` so the
    multi-engine consensus has 4 separate inputs to try."""
    pytest.importorskip("cv2")
    from aakaar_caps.cheque.micr import enhance_strip_variants

    src = _make_png(400, 80)
    variants = enhance_strip_variants(src)
    names = [name for name, _ in variants]
    assert "original" in names
    assert "upscale3x" in names
    assert "otsu_upscale" in names
    assert "sharpen" in names


def test_enhance_strip_variants_respects_custom_upscale() -> None:
    """The upscale factor is exposed so a slow box can drop to 2×
    and a powerful one can push to 4×. Variant names reflect the
    factor so the diagnostic log shows what actually ran."""
    pytest.importorskip("cv2")
    from aakaar_caps.cheque.micr import enhance_strip_variants

    src = _make_png(300, 60)
    variants = enhance_strip_variants(src, upscale=2)
    names = [name for name, _ in variants]
    assert "upscale2x" in names
    assert "upscale3x" not in names


# ---------------------------------------------------------------------------
# run_micr_ocr — end-to-end with mocked engines
# ---------------------------------------------------------------------------


def test_run_micr_ocr_returns_empty_when_no_backend(monkeypatch) -> None:
    """RapidOCR unavailable → MicrResult with empty text / parsed /
    regions but the variants_tried tuple is still populated so we
    can log what we attempted."""
    pytest.importorskip("cv2")
    from aakaar_caps.cheque import micr, rapid_ocr

    monkeypatch.setattr(rapid_ocr, "missing_dep", lambda: "rapidocr not installed")

    result = micr.run_micr_ocr(_make_png(1000, 500))
    assert result.text == ""
    assert result.parsed == {}
    assert result.regions == ()
    # We DID crop + enhance — the variants_tried tuple records that
    # so the diagnostic log shows we got as far as preprocessing.
    assert "original" in result.variants_tried


def test_run_micr_ocr_merges_unique_runs(monkeypatch) -> None:
    """RapidOCR reads the cheque number, the city-bank-branch run, and
    the TC. The union should contain ALL of them and the parser should
    populate cheque_no AND city/bank/branch/tc from it."""
    pytest.importorskip("cv2")
    from aakaar_caps.cheque import micr, rapid_ocr
    from aakaar_caps.cheque.rapid_ocr import OcrRegion

    monkeypatch.setattr(rapid_ocr, "missing_dep", lambda: None)
    monkeypatch.setattr(
        rapid_ocr, "run_ocr_detail",
        lambda _png: [
            OcrRegion(text="378781", confidence=0.92, bbox=[]),
            OcrRegion(text="607060202", confidence=0.81, bbox=[]),
            OcrRegion(text="30", confidence=0.88, bbox=[]),
        ],
    )

    result = micr.run_micr_ocr(_make_png(1000, 500))

    # All three distinct OCR runs are in the union.
    assert "378781" in result.text
    assert "607060202" in result.text
    assert "30" in result.text

    # Parser pulled out the full CTS layout from the union — the
    # specific bug this whole pipeline exists to fix.
    assert result.parsed == {
        "cheque_no": "378781",
        "city": "607",
        "bank": "060",
        "branch": "202",
        "tc": "30",
    }


def test_run_micr_ocr_deduplicates_across_variants(monkeypatch) -> None:
    """RapidOCR runs once per enhancement variant, so the same text
    comes back multiple times. We shouldn't return it N×; verify the
    dedup keeps a single highest-confidence copy."""
    pytest.importorskip("cv2")
    from aakaar_caps.cheque import micr, rapid_ocr
    from aakaar_caps.cheque.rapid_ocr import OcrRegion

    monkeypatch.setattr(rapid_ocr, "missing_dep", lambda: None)
    monkeypatch.setattr(
        rapid_ocr, "run_ocr_detail",
        lambda _png: [OcrRegion(text="378781", confidence=0.95, bbox=[])],
    )

    result = micr.run_micr_ocr(_make_png(1000, 500))
    assert result.text == "378781"
    # Single deduped copy at the highest confidence seen.
    assert result.regions == (("378781", 0.95),)


def test_run_micr_ocr_parses_serial_by_position_not_confidence(monkeypatch) -> None:
    """Regression: the cheque serial is printed in the angular MICR
    E-13B font with a leading zero, so the recognizer is LEAST
    confident about it. Parsing must follow LEFT-TO-RIGHT bbox
    position, not OCR confidence — otherwise a more-confident
    account-suffix run masquerades as the serial.

    Layout (left→right): serial '017424', city-bank-branch
    '534259502', account suffix '156700', tc '13'. We give the
    account suffix the HIGHEST confidence and the serial the LOWEST
    to prove confidence no longer drives the serial pick."""
    pytest.importorskip("cv2")
    from aakaar_caps.cheque import micr, rapid_ocr
    from aakaar_caps.cheque.rapid_ocr import OcrRegion

    monkeypatch.setattr(rapid_ocr, "missing_dep", lambda: None)

    def _box(x0: float, x1: float) -> list[list[float]]:
        return [[x0, 0.0], [x1, 0.0], [x1, 10.0], [x0, 10.0]]

    monkeypatch.setattr(
        rapid_ocr, "run_ocr_detail",
        lambda _png: [
            OcrRegion(text="017424", confidence=0.55, bbox=_box(10, 110)),
            OcrRegion(text="534259502", confidence=0.80, bbox=_box(150, 290)),
            OcrRegion(text="156700", confidence=0.97, bbox=_box(330, 430)),
            OcrRegion(text="13", confidence=0.70, bbox=_box(470, 500)),
        ],
    )

    result = micr.run_micr_ocr(_make_png(1000, 500))
    assert result.parsed.get("cheque_no") == "017424"
    assert result.parsed.get("city") == "534"
    assert result.parsed.get("bank") == "259"
    assert result.parsed.get("branch") == "502"
    assert result.parsed.get("tc") == "13"
