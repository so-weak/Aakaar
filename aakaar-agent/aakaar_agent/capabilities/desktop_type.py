"""cap.desktop_type — type text into the focused window (needs a GUI session)."""

from __future__ import annotations

from typing import Any

REF = "cap.desktop_type"
VERSION = "1"
GUI = True


async def run(inputs: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
    try:
        import pyautogui
    except Exception as e:  # pragma: no cover - needs the gui extra + a display
        raise RuntimeError("desktop_type requires the 'gui' extra (pyautogui)") from e
    text = inputs.get("text", "")
    interval = float(inputs.get("interval_ms", 0)) / 1000.0
    pyautogui.write(text, interval=interval)
    return {"typed": len(text)}
