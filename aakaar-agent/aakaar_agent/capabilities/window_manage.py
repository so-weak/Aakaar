"""cap.window_manage — list / focus / minimize / maximize / close windows (GUI)."""

from __future__ import annotations

from typing import Any

REF = "cap.window_manage"
VERSION = "1"
GUI = True


async def run(inputs: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
    try:
        import pygetwindow as gw
    except Exception as e:  # pragma: no cover - needs the gui extra + a display
        raise RuntimeError("window_manage requires the 'gui' extra (pygetwindow)") from e
    action = inputs.get("action", "list")
    title = inputs.get("title")
    if action == "list":
        return {"ok": True, "windows": [w.title for w in gw.getAllWindows() if w.title]}
    wins = gw.getWindowsWithTitle(title) if title else []
    if not wins:
        raise RuntimeError(f"no window matching {title!r}")
    w = wins[0]
    {"focus": w.activate, "minimize": w.minimize, "maximize": w.maximize, "close": w.close}.get(
        action, w.activate
    )()
    return {"ok": True, "windows": None}
