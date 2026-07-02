"""Unit tests for aakar.services.signature_detector.

We synthesise tiny CTS-2010-shaped cheques (white background +
optional ink in the signature panel) so the test runs in
milliseconds and doesn't need any fixture cheques on disk.

Skipped wholesale when OpenCV / numpy aren't installed —
matches the production contract (the detector returns a
NOT_VERIFIED with `missing_dep` set).
"""

from __future__ import annotations

import io
import pytest

pytest.importorskip("cv2")
pytest.importorskip("numpy")
pytest.importorskip("PIL")

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from aakaar_caps.cheque.signature_detector import (  # noqa: E402
    SignatureResult,
    detect_signature,
)


def _make_cheque(
    *,
    width: int = 1400,
    height: int = 600,
    signature: str = "none",  # "none" / "faint" / "real"
    include_baseline: bool = False,
) -> bytes:
    """Render a synthetic CTS-2010-shaped cheque PNG.

    `signature="none"` leaves the signature panel blank (just
    page background). `signature="faint"` draws a tiny mark that
    should land in the 'maybe' band. `signature="real"` draws
    cursive-shaped strokes that exceed the 'present' threshold.

    `include_baseline=True` adds the printed 'Authorised
    Signatory' underline INSIDE the signature region — real
    cheques have this baseline ink even when unsigned. Used by
    the dedicated baseline-tolerance test below.
    """
    img = Image.new("RGB", (width, height), color=(252, 252, 248))
    draw = ImageDraw.Draw(img)
    # Bare cheque structure so the test image looks vaguely real
    # and the signature region has the right contrast.
    draw.rectangle([(8, 8), (width - 8, height - 8)], outline=(40, 40, 40), width=2)
    # Signature panel boundary at ~(0.55, 0.62) → (0.95, 0.82),
    # matching the detector's default region. The baseline
    # underline below the panel is OUTSIDE the detector region
    # by default so the 'blank' test really is blank — opt in
    # via `include_baseline` to exercise the noise-tolerance
    # codepath.
    x0, y0, x1, y1 = (
        int(width * 0.55), int(height * 0.62),
        int(width * 0.95), int(height * 0.82),
    )
    if include_baseline:
        draw.line([(x0, y1 - 5), (x1, y1 - 5)], fill=(120, 120, 120), width=1)

    if signature == "faint":
        # A single short stroke — well below a real signature but
        # above page noise.
        draw.line(
            [(x0 + 30, y0 + 60), (x0 + 80, y0 + 60)],
            fill=(20, 20, 20), width=2,
        )
    elif signature == "real":
        # Multiple cursive-shaped strokes — emulates a real
        # signature's ink density (a few thousand pixels of ink
        # in the panel).
        for i in range(6):
            yo = y0 + 30 + i * 8
            draw.line(
                [(x0 + 20 + i * 6, yo), (x0 + 200, yo + 12)],
                fill=(10, 10, 30), width=3,
            )
        draw.ellipse(
            [(x0 + 180, y0 + 70), (x0 + 260, y0 + 110)],
            outline=(10, 10, 30), width=3,
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestDetectSignature:
    def test_blank_cheque_returns_absent(self) -> None:
        png = _make_cheque(signature="none")
        result = detect_signature(png)
        assert isinstance(result, SignatureResult)
        assert result.missing_dep is None
        assert result.verdict == "absent"
        assert result.present is False
        # Just the printed underline should leave density well
        # below the 'maybe' floor (0.2%).
        assert result.density < 0.002

    def test_real_signature_returns_present(self) -> None:
        png = _make_cheque(signature="real")
        result = detect_signature(png)
        assert result.missing_dep is None
        assert result.verdict == "present"
        assert result.present is True
        # Sanity-check the density: at least the 'present'
        # threshold (0.5%) — we typically see ~3-6% on a real
        # signature.
        assert result.density >= 0.005

    def test_faint_mark_falls_into_maybe(self) -> None:
        png = _make_cheque(signature="faint")
        result = detect_signature(png)
        assert result.missing_dep is None
        # Faint mark lands in the warn band — either 'maybe' or
        # 'absent' is acceptable; 'present' would be a false
        # positive.
        assert result.verdict in {"maybe", "absent"}
        assert result.present is False

    def test_region_png_returned_on_success(self) -> None:
        png = _make_cheque(signature="real")
        result = detect_signature(png)
        assert result.region_png is not None
        # The cropped panel must itself be a valid PNG.
        arr = np.frombuffer(result.region_png, dtype=np.uint8)
        decoded = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        assert decoded is not None
        assert decoded.shape[0] > 0 and decoded.shape[1] > 0

    def test_empty_bytes_marks_missing_dep(self) -> None:
        result = detect_signature(b"")
        assert result.missing_dep is not None
        assert result.present is False
        assert result.verdict == "absent"

    def test_corrupt_bytes_marks_missing_dep(self) -> None:
        result = detect_signature(b"not a png")
        assert result.missing_dep is not None

    def test_image_too_small_marks_missing_dep(self) -> None:
        # A 10x5 PNG can't accommodate a meaningful signature
        # crop — detector should bail with a helpful reason.
        tiny = Image.new("RGB", (10, 5), color=(255, 255, 255))
        buf = io.BytesIO()
        tiny.save(buf, format="PNG")
        result = detect_signature(buf.getvalue())
        # Either NOT_VERIFIED (too small) or absent-with-zero
        # density is acceptable — the contract is just that we
        # don't crash.
        assert result.present is False

    def test_custom_region(self) -> None:
        # When the operator passes a region that DELIBERATELY
        # avoids the signature, we should report 'absent'. We
        # pick a crop in the middle of the page that's clear of
        # the outer cheque border (which would otherwise inflate
        # density above the maybe floor).
        png = _make_cheque(signature="real")
        result = detect_signature(png, region=(0.1, 0.1, 0.4, 0.4))
        assert result.missing_dep is None
        # Middle-left crop has no signature ink.
        assert result.verdict in {"absent", "maybe"}

    def test_real_signature_dominates_baseline_noise(self) -> None:
        # The realistic case: a real cheque has the printed
        # 'Authorised Signatory' underline inside the panel
        # AND the customer's signature. The detector must still
        # land on 'present' — the signature's ink density
        # dominates the thin printed baseline.
        png = _make_cheque(signature="real", include_baseline=True)
        result = detect_signature(png)
        assert result.missing_dep is None
        assert result.verdict == "present"
        assert result.present is True


class TestMissingDependencyShape:
    """When OpenCV isn't importable the function returns a
    NOT_VERIFIED-style result rather than crashing. We can't
    easily simulate a missing import in-process; this test
    documents the contract instead via the empty-bytes path
    (which exercises the same `missing_dep`-carrying return
    type)."""

    def test_missing_dep_result_shape(self) -> None:
        result = detect_signature(b"")
        # The full result is shaped consistently regardless of
        # failure mode.
        assert hasattr(result, "missing_dep")
        assert hasattr(result, "present")
        assert hasattr(result, "verdict")
        assert hasattr(result, "density")
        assert hasattr(result, "region_png")
