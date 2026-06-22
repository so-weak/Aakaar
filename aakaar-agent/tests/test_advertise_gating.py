"""The agent must not advertise browser caps it can't actually run (Chromium
launch probe failed / disabled), so the server never routes browser work to a
half-installed agent."""

from __future__ import annotations

from aakaar_agent.capabilities import load_capabilities
from aakaar_agent.client import AgentClient


def test_browser_caps_hidden_when_probe_failed() -> None:
    load_capabilities()
    c = AgentClient("ws://x/ws/agents", "id.secret")
    c._browser_ok = False
    refs = {cap["ref"] for cap in c._advertised_caps()}
    # Stateless utility/desktop caps remain; browser-family caps are dropped.
    assert "cap.shell_exec" in refs
    assert not any(r.startswith("browser.") for r in refs)
    assert "cap.web_login" not in refs and "cap.screenshot" not in refs


def test_browser_caps_present_when_probe_ok() -> None:
    load_capabilities()
    c = AgentClient("ws://x/ws/agents", "id.secret")
    c._browser_ok = True
    refs = {cap["ref"] for cap in c._advertised_caps()}
    assert "browser.open_session" in refs and "cap.web_login" in refs
