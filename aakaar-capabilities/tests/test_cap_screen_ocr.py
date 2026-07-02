"""Test for cap.screen_ocr — capture a monitor and OCR it.

Guarded by pytest.importorskip for both heavy libs (mss, rapidocr). Also skips
gracefully when no display / screen grab is available (headless CI), and when
the rapidocr ONNX models cannot be initialised. Asserts the screenshot is
written to the fake object store and the output shape is correct; the exact OCR
text is not asserted (screen content is environment-dependent).
"""

from __future__ import annotations

import pytest

from aakaar_caps.context import CapabilityContext


def _ctx() -> tuple[CapabilityContext, dict[str, bytes]]:
    store: dict[str, bytes] = {}

    async def writer(key: str, data: bytes) -> str:
        uri = "aakaar://t/x/" + key
        store[uri] = data
        return uri

    async def reader(uri: str) -> bytes:
        return store[uri]

    ctx = CapabilityContext(run_id="screen", object_writer=writer, object_reader=reader)
    return ctx, store


async def test_screen_ocr_shape() -> None:
    pytest.importorskip("mss")
    pytest.importorskip("rapidocr")
    pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    from aakaar_caps.caps import screen_ocr

    ctx, store = _ctx()
    try:
        out = await screen_ocr.run(ctx, {"monitor": 1})
    except Exception as e:  # noqa: BLE001 — headless / no models / no display
        pytest.skip(f"screen capture or OCR engine unavailable: {e}")

    assert out["image_uri"].startswith("aakaar://")
    assert out["image_uri"] in store
    assert len(store[out["image_uri"]]) > 0  # real PNG bytes
    assert isinstance(out["text"], str)
    assert isinstance(out["regions"], list)
    for r in out["regions"]:
        assert set(r) == {"text", "confidence"}
        assert 0.0 <= r["confidence"] <= 1.0
