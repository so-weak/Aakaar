"""cap.screen_ocr — capture the host screen and OCR it with PP-OCRv5.

Grabs a screenshot of a monitor (or a sub-region of it) with ``mss``, saves the
PNG to managed storage via ``ctx.write_object``, then runs PP-OCRv5 (via the
``rapidocr`` ONNX package — bundled models, offline, CPU) over the capture and
returns the joined text plus per-region ``{text, confidence}``.

Only reads the screen and writes the capture to managed storage — no host
mutation — so ``side_effecting=False``. The heavy deps (``mss``, ``rapidocr``,
``numpy``, ``PIL``) are imported lazily so the server can register the
capability without them; the executing host must have them installed.
"""

from __future__ import annotations

import io
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.screen_ocr"

_ENGINE: Any = None  # cached PP-OCRv5 engine (per process)


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    region: tuple[int, int, int, int] | None = Field(
        default=None,
        description="Optional [left, top, width, height] sub-region of the monitor to capture. Omit for the full monitor.",
    )
    monitor: int = Field(
        default=1, ge=0,
        description="mss monitor index (1 = primary; 0 = the virtual all-monitors bounding box).",
    )


class _Region(BaseModel):
    text: str
    confidence: float


class _Outputs(BaseModel):
    text: str = Field(description="All recognised text, joined by newlines.")
    regions: list[_Region] = Field(description="Per-detection {text, confidence}.")
    image_uri: str = Field(description="Managed-storage URI (aakaar://...) of the saved screenshot PNG.")


SPEC = CapabilitySpec(
    ref=CAP_REF,
    description=(
        "Capture the executing host's screen (a whole monitor, or a [left,top,width,height] "
        "sub-region) with mss, save the PNG to managed storage, and OCR it with PP-OCRv5 "
        "(rapidocr, offline CPU). Returns the joined text, per-detection {text,confidence}, "
        "and the screenshot URI. Read-only (only captures and stores)."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("screen", "ocr"),
    side_effecting=False,
)


def _engine() -> Any:
    global _ENGINE
    if _ENGINE is None:
        try:
            from rapidocr import RapidOCR  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised only when dep absent
            raise RuntimeError(
                "cap.screen_ocr needs rapidocr — install aakaar-capabilities[automation]"
            ) from exc
        _ENGINE = RapidOCR()
    return _ENGINE


def _capture(region: Any, monitor: int) -> bytes:
    """Return PNG bytes of the requested screen area via mss."""
    try:
        import mss  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - exercised only when dep absent
        raise RuntimeError(
            "cap.screen_ocr needs mss — install aakaar-capabilities[automation]"
        ) from exc
    from PIL import Image

    with mss.mss() as sct:
        if region is not None:
            left, top, width, height = (int(v) for v in region)
            bbox = {"left": left, "top": top, "width": width, "height": height}
        else:
            bbox = sct.monitors[monitor]
        shot = sct.grab(bbox)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _run_ppocrv5(eng: Any, arr: Any) -> list[tuple[str, float]]:
    """Parse PP-OCRv5 output into (text, confidence) pairs (mirrors ocr_account_number)."""
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


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    import numpy as np
    from PIL import Image

    region = inputs.get("region")
    monitor = int(inputs.get("monitor", 1))

    logger.info("cap.screen_ocr start run_id=%s monitor=%s region=%s", ctx.run_id, monitor, region)
    png = _capture(region, monitor)
    image_uri = await ctx.write_object("screen.png", png)

    arr = np.array(Image.open(io.BytesIO(png)).convert("RGB"))
    eng = _engine()
    regions = [{"text": t, "confidence": round(s, 4)} for t, s in _run_ppocrv5(eng, arr)]
    text = "\n".join(r["text"] for r in regions)

    logger.info("cap.screen_ocr ok run_id=%s regions=%d uri=%s", ctx.run_id, len(regions), image_uri)
    return {"text": text, "regions": regions, "image_uri": image_uri}
