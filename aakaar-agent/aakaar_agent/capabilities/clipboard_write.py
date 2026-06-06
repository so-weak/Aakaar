"""cap.clipboard_write — set the host clipboard (needs a GUI session)."""

from __future__ import annotations

from typing import Any

REF = "cap.clipboard_write"
VERSION = "1"
GUI = True


async def run(inputs: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
    try:
        import pyperclip
    except Exception as e:  # pragma: no cover - needs the gui extra
        raise RuntimeError("clipboard_write requires the 'gui' extra (pyperclip)") from e
    pyperclip.copy(str(inputs.get("text", "")))
    return {"ok": True}
