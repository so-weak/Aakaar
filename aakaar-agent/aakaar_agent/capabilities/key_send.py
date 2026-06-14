"""cap.key_send — press a validated key combo (needs a GUI session).

inputs:  {combo: str} e.g. "enter", "ctrl+s", "alt+tab", "ctrl+shift+t"
outputs: {sent: str} — the canonical combo that was pressed

The combo grammar is strict on purpose (this is a compile target for recorded
workflows, not a free-form injector): up to three distinct modifiers from
ctrl/alt/shift/cmd/win plus exactly one terminal key, which must be a named
navigation/function key or a single ASCII letter/digit. Anything else raises
ValueError before any GUI library is touched.
"""

from __future__ import annotations

from typing import Any

REF = "cap.key_send"
VERSION = "1"
GUI = True

_MAX_COMBO_LEN = 40
_MODIFIER_ORDER = ("ctrl", "alt", "shift", "cmd", "win")
_NAMED_KEYS = frozenset(
    {
        "enter", "tab", "esc", "space", "backspace", "delete", "insert",
        "up", "down", "left", "right", "home", "end", "pageup", "pagedown",
        "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
    }
)
# pyautogui spells the macOS/Windows super keys differently.
_PYAUTOGUI_NAMES = {"cmd": "command", "win": "winleft"}
_CANONICAL_NAMES = {v: k for k, v in _PYAUTOGUI_NAMES.items()}


def parse_combo(combo: Any) -> list[str]:
    """Validate ``combo`` and return the key list for pyautogui.hotkey().
    Raises ValueError on anything outside the grammar."""
    if not isinstance(combo, str) or not combo.strip():
        raise ValueError("combo must be a non-empty string")
    if len(combo) > _MAX_COMBO_LEN:
        raise ValueError("combo too long")
    parts = [p.strip().lower() for p in combo.split("+")]
    if any(not p for p in parts):
        raise ValueError(f"malformed combo {combo!r}")
    *mods, key = parts
    if len(mods) > 3 or len(set(mods)) != len(mods):
        raise ValueError(f"too many or repeated modifiers in {combo!r}")
    for mod in mods:
        if mod not in _MODIFIER_ORDER:
            raise ValueError(f"unknown modifier {mod!r} in {combo!r}")
    if key in _MODIFIER_ORDER:
        raise ValueError(f"combo {combo!r} has no terminal key")
    if key not in _NAMED_KEYS and not (len(key) == 1 and key.isascii() and key.isalnum()):
        raise ValueError(f"unsupported key {key!r} in {combo!r}")
    ordered = [m for m in _MODIFIER_ORDER if m in mods]
    return [_PYAUTOGUI_NAMES.get(k, k) for k in [*ordered, key]]


async def run(inputs: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
    keys = parse_combo(inputs.get("combo"))
    try:
        import pyautogui
    except Exception as e:  # pragma: no cover - needs the gui extra + a display
        raise RuntimeError("key_send requires the 'gui' extra (pyautogui)") from e
    pyautogui.hotkey(*keys)
    return {"sent": "+".join(_CANONICAL_NAMES.get(k, k) for k in keys)}
