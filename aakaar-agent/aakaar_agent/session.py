"""Detect the host OS and whether an interactive GUI session is available.

GUI/desktop automation only works with a logged-in display. The server uses the
reported ``gui`` flag so it never places a GUI-tagged node on a headless agent.
"""

from __future__ import annotations

import os
import platform


def detect_os() -> str:
    return platform.system().lower()  # "windows" | "darwin" | "linux"


def detect_gui() -> bool:
    system = platform.system()
    if system == "Darwin":
        # A LaunchAgent runs in the user's GUI session; a LaunchDaemon does not.
        # We can't perfectly tell here, so assume GUI unless explicitly headless.
        return os.environ.get("AAKAAR_AGENT_HEADLESS", "").lower() not in ("1", "true")
    if system == "Windows":
        # Session 0 (service) is non-interactive; a user session has SESSIONNAME.
        name = os.environ.get("SESSIONNAME", "").lower()
        return name not in ("", "services")
    # Linux/other: a GUI session exposes an X11 DISPLAY or a Wayland socket.
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
