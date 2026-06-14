"""Agent capability handlers.

shell_exec and system_info are *shared-library* capabilities (defined once in
``aakaar_caps`` and run on the server or an agent). The agent loads them via the
``_SharedCap`` adapter, so we exercise them through the real dispatch path rather
than importing a local module — there is no longer an agent-local copy. GUI
capabilities can't be driven without a display, so we assert their graceful
contract: a clear RuntimeError when the optional GUI deps aren't present.
"""

from __future__ import annotations

import sys

import pytest

from aakaar_agent import capabilities
from aakaar_agent.capabilities import (
    clipboard_write,
    desktop_click,
    desktop_type,
)


def test_load_and_advertise() -> None:
    reg = capabilities.load_capabilities()
    refs = {r["ref"] for r in capabilities.advertised()}
    assert "cap.shell_exec" in refs
    assert "cap.system_info" in refs
    assert "cap.desktop_click" in refs
    assert "cap.desktop_scroll" in refs
    assert "cap.key_send" in refs
    assert "cap.activity_recording" in refs
    assert reg["cap.shell_exec"].GUI is False
    assert reg["cap.desktop_click"].GUI is True
    assert reg["cap.activity_recording"].GUI is True


def test_shared_caps_come_from_the_shared_library() -> None:
    """shell_exec/system_info must resolve to the shared-lib adapter, not a
    re-introduced agent-local copy (guards the dedup)."""
    reg = capabilities.load_capabilities()
    assert isinstance(reg["cap.shell_exec"], capabilities._SharedCap)
    assert isinstance(reg["cap.system_info"], capabilities._SharedCap)


async def test_shell_exec_runs_argv_via_dispatch() -> None:
    capabilities.load_capabilities()
    out = await capabilities.dispatch(
        "cap.shell_exec", {"argv": [sys.executable, "-c", "print('hello-agent')"]}, {}
    )
    assert out["exit_code"] == 0
    assert "hello-agent" in out["stdout"]


async def test_shell_exec_rejects_bad_argv() -> None:
    capabilities.load_capabilities()
    with pytest.raises(ValueError, match="argv"):
        await capabilities.dispatch("cap.shell_exec", {"argv": "echo hi"}, {})


async def test_shell_exec_dispatch_via_registry() -> None:
    capabilities.load_capabilities()
    out = await capabilities.dispatch(
        "cap.shell_exec", {"argv": [sys.executable, "-c", "print(2 + 2)"]}, {}
    )
    assert out["stdout"].strip() == "4"


async def test_system_info_reports_os() -> None:
    capabilities.load_capabilities()
    out = await capabilities.dispatch("cap.system_info", {}, {})
    assert out["os"]
    # memory/cpu present only if psutil is installed; os is always returned.


def _gui_unavailable(mod_name: str) -> bool:
    try:
        __import__(mod_name)
        return False
    except Exception:
        return True


async def test_desktop_click_requires_gui_extra() -> None:
    if not _gui_unavailable("pyautogui"):
        pytest.skip("pyautogui present; cannot exercise a headless click")
    with pytest.raises(RuntimeError, match="gui"):
        await desktop_click.run({"x": 1, "y": 1}, {})


async def test_desktop_type_requires_gui_extra() -> None:
    if not _gui_unavailable("pyautogui"):
        pytest.skip("pyautogui present")
    with pytest.raises(RuntimeError, match="gui"):
        await desktop_type.run({"text": "hi"}, {})


async def test_clipboard_write_requires_gui_extra() -> None:
    if not _gui_unavailable("pyperclip"):
        pytest.skip("pyperclip present")
    with pytest.raises(RuntimeError, match="gui"):
        await clipboard_write.run({"text": "hi"}, {})


async def test_dispatch_unknown_ref() -> None:
    capabilities.load_capabilities()
    with pytest.raises(KeyError):
        await capabilities.dispatch("cap.nope", {}, {})
