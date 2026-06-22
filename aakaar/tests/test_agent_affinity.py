"""Stage 6 — session affinity in the agent registry.

A run that opens a browser session on one agent must keep landing on that SAME
agent; if the agent goes away, placement fails fast (a browser session is not
resumable onto a fresh agent) rather than silently re-routing.
"""

from __future__ import annotations

import uuid

import pytest

from aakaar.workers.remote.protocol import AgentCapability, AgentInfo
from aakaar.workers.remote.registry import (
    AgentRegistry,
    NoAgentAvailable,
    SessionAgentGone,
)


class _Conn:
    def __init__(self, alias: str, tenant: uuid.UUID, refs: list[str]) -> None:
        self.info = AgentInfo(
            alias=alias,
            tenant_id=tenant,
            pools=("all",),
            capabilities=tuple(AgentCapability(ref=r) for r in refs),
        )

    async def close(self) -> None:  # pragma: no cover
        pass


def test_run_pins_to_one_agent_across_nodes() -> None:
    tenant = uuid.uuid4()
    reg = AgentRegistry()
    a = _Conn("a-mac", tenant, ["browser.open_session", "browser.navigate", "cap.web_login"])
    b = _Conn("b-mac", tenant, ["browser.open_session", "browser.navigate", "cap.web_login"])
    reg.register(a)
    reg.register(b)

    # First (sticky) browser node pins the run; deterministic pick is "a-mac".
    first = reg.resolve(tenant, "all", ref="browser.open_session", run_id="R", sticky=True)
    # Even though both agents could serve navigate, affinity keeps it on a-mac.
    second = reg.resolve(tenant, "all", ref="browser.navigate", run_id="R", sticky=True)
    assert first.info.alias == second.info.alias == "a-mac"


def test_affinity_fails_fast_when_pinned_agent_gone() -> None:
    tenant = uuid.uuid4()
    reg = AgentRegistry()
    a = _Conn("a-mac", tenant, ["browser.open_session", "browser.navigate"])
    reg.register(a)
    reg.resolve(tenant, "a-mac", ref="browser.open_session", run_id="R", sticky=True)
    # The agent holding the session drops.
    reg.unregister(tenant, "a-mac")
    with pytest.raises(SessionAgentGone):
        reg.resolve(tenant, "a-mac", ref="browser.navigate", run_id="R", sticky=True)


def test_release_run_drops_binding() -> None:
    tenant = uuid.uuid4()
    reg = AgentRegistry()
    a = _Conn("a-mac", tenant, ["browser.open_session"])
    reg.register(a)
    reg.resolve(tenant, "a-mac", ref="browser.open_session", run_id="R", sticky=True)
    assert reg.release_run(tenant, "R") == "a-mac"
    assert reg.release_run(tenant, "R") is None  # idempotent


def test_unsticky_node_does_not_pin() -> None:
    tenant = uuid.uuid4()
    reg = AgentRegistry()
    a = _Conn("a-mac", tenant, ["cap.json_extract"])
    reg.register(a)
    # A non-browser node with run_id but sticky=False must not create a binding.
    reg.resolve(tenant, "a-mac", ref="cap.json_extract", run_id="R", sticky=False)
    assert reg.release_run(tenant, "R") is None
