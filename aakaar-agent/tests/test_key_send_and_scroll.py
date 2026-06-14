"""cap.key_send combo grammar + cap.desktop_scroll validation."""

from __future__ import annotations

import pytest

from aakaar_agent.capabilities import desktop_scroll, key_send


@pytest.mark.parametrize(
    ("combo", "keys"),
    [
        ("enter", ["enter"]),
        ("ctrl+s", ["ctrl", "s"]),
        ("Ctrl + S", ["ctrl", "s"]),  # case/space tolerant
        ("alt+tab", ["alt", "tab"]),
        ("shift+ctrl+t", ["ctrl", "shift", "t"]),  # canonical modifier order
        ("cmd+c", ["command", "c"]),  # pyautogui spelling
        ("win+e", ["winleft", "e"]),
        ("f5", ["f5"]),
        ("ctrl+alt+delete", ["ctrl", "alt", "delete"]),
        ("9", ["9"]),
    ],
)
def test_parse_combo_accepts_safe_grammar(combo: str, keys: list[str]) -> None:
    assert key_send.parse_combo(combo) == keys


@pytest.mark.parametrize(
    "combo",
    [
        None,
        42,
        "",
        "   ",
        "+",
        "ctrl+",
        "+s",
        "ctrl++v",
        "ctrl+ctrl+a",  # repeated modifier
        "ctrl+shift+alt+cmd+a",  # too many modifiers
        "ctrl+alt",  # no terminal key
        "hello",  # multi-char non-named key
        "ctrl+rm -rf /",
        "meta+a",  # unknown modifier
        "a+b",  # 'a' is not a modifier
        "ctrl+ä",  # non-ascii key
        "ctrl+" + "x" * 60,  # too long
    ],
)
def test_parse_combo_rejects_garbage(combo: object) -> None:
    with pytest.raises(ValueError):
        key_send.parse_combo(combo)


def _gui_unavailable() -> bool:
    try:
        import pyautogui  # noqa: F401

        return False
    except Exception:
        return True


async def test_key_send_validates_before_touching_gui() -> None:
    # garbage must be rejected even when pyautogui is missing/present
    with pytest.raises(ValueError):
        await key_send.run({"combo": "ctrl+ctrl+a"}, {})


async def test_key_send_requires_gui_extra() -> None:
    if not _gui_unavailable():
        pytest.skip("pyautogui present; cannot exercise a headless refusal")
    with pytest.raises(RuntimeError, match="gui"):
        await key_send.run({"combo": "ctrl+s"}, {})


@pytest.mark.parametrize(
    "inputs",
    [
        {},
        {"dx": 0, "dy": 0},
        {"dy": "three"},
        {"dy": 5001},
        {"dx": -999999},
    ],
)
async def test_desktop_scroll_rejects_bad_deltas(inputs: dict) -> None:
    with pytest.raises(ValueError):
        await desktop_scroll.run(inputs, {})


async def test_desktop_scroll_requires_gui_extra() -> None:
    if not _gui_unavailable():
        pytest.skip("pyautogui present; cannot exercise a headless refusal")
    with pytest.raises(RuntimeError, match="gui"):
        await desktop_scroll.run({"dy": -3}, {})
