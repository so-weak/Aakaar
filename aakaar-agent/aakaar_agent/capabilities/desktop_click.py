"""cap.desktop_click — click by coordinates or matched image (needs a GUI session)."""

from __future__ import annotations

from typing import Any

REF = "cap.desktop_click"
VERSION = "1"
GUI = True


async def run(inputs: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
    try:
        import pyautogui
    except Exception as e:  # pragma: no cover - needs the gui extra + a display
        raise RuntimeError("desktop_click requires the 'gui' extra (pyautogui)") from e
    image = inputs.get("image")
    button = inputs.get("button", "left")
    if image:
        loc = pyautogui.locateCenterOnScreen(image, confidence=0.8)
        if loc is None:
            raise RuntimeError(f"image {image!r} not found on screen")
        pyautogui.click(loc.x, loc.y, button=button)
    else:
        x, y = inputs.get("x"), inputs.get("y")
        if x is None or y is None:
            raise ValueError("desktop_click needs (x, y) or image")
        pyautogui.click(int(x), int(y), button=button)
    return {"clicked": True}
