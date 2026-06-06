"""Aakaar remote execution agent.

A lightweight, standalone process deployed to a workstation. It dials OUT to the
Aakaar server over an authenticated WebSocket, advertises its OS / GUI session /
capabilities, and executes capability nodes the server dispatches to it. The
server stays the orchestrator; this agent is a capability executor.
"""

VERSION = "0.1.0"
