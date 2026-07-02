"""Tests for the ported CTS cheque-verification cap wrappers:
  - cap.cheque_extract    (OCR one side -> JSON-safe ChequeFields dict)
  - cap.cheque_verify     (end-to-end extract -> validate -> decide)
  - cap.micr_read         (MICR strip OCR)
  - cap.signature_detect  (signature presence, region_png dropped)

The OCR-driven tests build a fake CapabilityContext whose object_reader returns a
tiny in-memory PNG and are guarded with importorskip("rapidocr") / ("cv2") so they
SKIP cleanly when the optional `cheque` deps are absent. The serializer tests are
pure-logic and always run.
"""

from __future__ import annotations

import io
from typing import Any

import pytest

from aakaar_caps.cheque._serialize import to_jsonsafe
from aakaar_caps.context import CapabilityContext


def _tiny_png() -> bytes:
    """A minimal blank white PNG (no OCR content expected — we assert the caps
    return the right shape and never raise, not that they read specific fields)."""
    pytest.importorskip("PIL")
    from PIL import Image  # noqa: PLC0415

    buf = io.BytesIO()
    Image.new("RGB", (240, 100), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _ctx_reading(data: bytes) -> CapabilityContext:
    async def _reader(_uri: str) -> bytes:
        return data

    return CapabilityContext(run_id="chq-test", object_reader=_reader)


# ------------------------------- _serialize (pure) ----------------------------
def test_to_jsonsafe_drops_bytes_and_flattens_tuples() -> None:
    """The serializer must strip raw bytes (e.g. region_png) at any depth and
    turn tuples into lists so the result is JSON-safe."""
    payload = {
        "verdict": "present",
        "region_png": b"\x89PNG-binary-bytes",
        "nested": {"blob": bytearray(b"more bytes"), "keep": 1},
        "runs": ("a", "b"),
        "mixed": [1, b"drop-me", 2],
    }
    out = to_jsonsafe(payload)
    assert out["verdict"] == "present"
    assert "region_png" not in out  # bytes dropped
    assert out["nested"] == {"keep": 1}  # bytearray dropped, keep survives
    assert out["runs"] == ["a", "b"]  # tuple -> list
    assert out["mixed"] == [1, 2]  # bytes element dropped

    import json  # noqa: PLC0415
    json.dumps(out)  # must be JSON-serializable (would raise on stray bytes)


def test_to_jsonsafe_uses_dataclass_to_dict() -> None:
    from dataclasses import dataclass  # noqa: PLC0415

    @dataclass
    class _Thing:
        name: str
        blob: bytes

        def to_dict(self) -> dict[str, Any]:
            return {"name": self.name, "blob": self.blob}

    out = to_jsonsafe(_Thing(name="x", blob=b"raw"))
    assert out == {"name": "x"}  # to_dict honored, bytes dropped


# ------------------------------- cap.cheque_extract ---------------------------
async def test_cap_cheque_extract_returns_jsonsafe_fields() -> None:
    pytest.importorskip("rapidocr")
    from aakaar_caps.caps import cheque_extract  # noqa: PLC0415

    ctx = _ctx_reading(_tiny_png())
    out = await cheque_extract.run(ctx, {"image_uri": "aakaar://t/x/front.png", "side": "front"})

    assert "fields" in out
    fields = out["fields"]
    assert isinstance(fields, dict)
    assert fields.get("side") == "front"
    # extract_fields never raises; on a blank image the named fields are just None.
    import json  # noqa: PLC0415
    json.dumps(out)  # JSON-safe


# ------------------------------- cap.cheque_verify ----------------------------
async def test_cap_cheque_verify_end_to_end_shape() -> None:
    pytest.importorskip("rapidocr")
    from aakaar_caps.caps import cheque_verify  # noqa: PLC0415

    ctx = _ctx_reading(_tiny_png())
    out = await cheque_verify.run(ctx, {"front_image_uri": "aakaar://t/x/front.png"})

    # Decision + report contract keys are all present and JSON-safe.
    assert set(out) >= {"status", "summary", "rejection_reason", "overall_status", "checks", "fields"}
    assert out["status"] in {"AUTO_APPROVE", "AUTO_REJECT", "NEEDS_REVIEW"}
    assert isinstance(out["checks"], list)
    assert out["fields"]["back"] is None  # no back image supplied
    assert isinstance(out["fields"]["front"], dict)

    import json  # noqa: PLC0415
    json.dumps(out)  # JSON-safe (no stray bytes / tuples)


# ------------------------------- cap.micr_read --------------------------------
async def test_cap_micr_read_shape() -> None:
    pytest.importorskip("rapidocr")
    pytest.importorskip("cv2")  # crop_bottom_strip / enhance variants need OpenCV
    from aakaar_caps.caps import micr_read  # noqa: PLC0415

    ctx = _ctx_reading(_tiny_png())
    out = await micr_read.run(ctx, {"image_uri": "aakaar://t/x/front.png"})

    assert set(out) == {"text", "parsed", "variants_tried"}
    assert isinstance(out["text"], str)
    assert isinstance(out["parsed"], dict)
    assert isinstance(out["variants_tried"], list)

    import json  # noqa: PLC0415
    json.dumps(out)  # JSON-safe


# ------------------------------- cap.signature_detect -------------------------
async def test_cap_signature_detect_drops_region_png() -> None:
    pytest.importorskip("cv2")
    from aakaar_caps.caps import signature_detect  # noqa: PLC0415

    ctx = _ctx_reading(_tiny_png())
    out = await signature_detect.run(ctx, {"image_uri": "aakaar://t/x/front.png"})

    assert set(out) == {"present", "density", "verdict"}  # region_png never returned
    assert isinstance(out["present"], bool)
    assert 0.0 <= out["density"] <= 1.0
    assert out["verdict"] in {"present", "maybe", "absent"}
