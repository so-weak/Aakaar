"""cap.desktop_scroll — scroll the focused window (needs a GUI session).

inputs:  {dx?: int, dy?: int} — wheel notches; positive dy scrolls up,
         positive dx scrolls right; at least one must be non-zero and both
         must be within +/-5000 (a recorded trace never legitimately exceeds
         that, so out-of-range values are rejected rather than clamped).
outputs: {scrolled: bool, dx: int, dy: int}
"""

from __future__ import annotations

from typing import Any

REF = "cap.desktop_scroll"
VERSION = "1"
GUI = True

_MAX_DELTA = 5000


def _delta(inputs: dict[str, Any], name: str) -> int:
    try:
        value = int(inputs.get(name) or 0)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    if abs(value) > _MAX_DELTA:
        raise ValueError(f"{name} out of range (max +/-{_MAX_DELTA})")
    return value


async def run(inputs: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
    dx, dy = _delta(inputs, "dx"), _delta(inputs, "dy")
    if not dx and not dy:
        raise ValueError("desktop_scroll needs a non-zero dx or dy")
    try:
        import pyautogui
    except Exception as e:  # pragma: no cover - needs the gui extra + a display
        raise RuntimeError("desktop_scroll requires the 'gui' extra (pyautogui)") from e
    if dy:
        pyautogui.scroll(dy)
    if dx:
        pyautogui.hscroll(dx)
    return {"scrolled": True, "dx": dx, "dy": dy}
