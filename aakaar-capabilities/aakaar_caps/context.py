"""The portable context a shared capability is handed.

Only this surface is guaranteed across hosts. ``secrets`` is always present
(empty if the capability needs none). The stateful/service fields below are
optional — the SERVER fills them from its ``ActivityContext`` (real browser
pool, object store, LLM, signal hub); a remote AGENT fills them from its local
runtime plus WS-RPC proxies back to the server. A host that can't provide one
leaves it ``None``, and using it raises ``CapabilityError`` (so a capability
that needs a service simply can't run where the service is absent, which
placement should already prevent).

The agent never holds the OpenAI key, the vault, or the canonical object store:
``text_completer``/``planner_completer``/``object_*`` are proxies that round-trip
to the server. ``browser_pool``/``session_state`` are the only genuinely local
state on the agent (the live Chromium lives there).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aakaar_caps.llm_types import LLMMessage


class CapabilityError(RuntimeError):
    """A capability could not run on this host (e.g. a service it needs is absent)."""


@dataclass
class CapabilityContext:
    secrets: dict[str, str] = field(default_factory=dict)
    tenant_id: str | None = None
    run_id: str | None = None
    node_id: str | None = None

    # Server-owned services, reached on the agent via WS-RPC proxies.
    object_reader: Callable[[str], Awaitable[bytes]] | None = None
    object_writer: Callable[[str, bytes], Awaitable[str]] | None = None
    text_completer: Callable[[str, str], str] | None = None
    planner_completer: Callable[[list[LLMMessage]], str] | None = None

    # Genuinely local state on whichever host runs the capability. The live
    # browser session lives here and is keyed into ``session_state`` so a later
    # node in the same run (on the same host) can look it up by id.
    browser_pool: Any = None
    session_state: dict[str, Any] = field(default_factory=dict)
    signals: Any = None

    # Human-in-the-loop seam: open a prompt (captcha/OTP/confirm) and await the
    # human's reply. Returns the response string. The server wires this to its
    # SignalHub; the agent proxies it over the back-channel to the server's hub
    # (the human answers in the same chat UI regardless of where the browser
    # runs). None when no HITL channel is available (unit tests).
    signal_opener: Callable[[str, str], Awaitable[str]] | None = None

    # Optional sibling-copy directory for downloads (a server-host dev
    # convenience, e.g. ~/Downloads). The object store is always the canonical
    # location. Set only on the server; an agent leaves it None so downloaded
    # files never persist on the agent's disk.
    download_mirror_dir: Any = None

    async def read_object(self, uri: str) -> bytes:
        if self.object_reader is None:
            raise CapabilityError("object storage is not available on this host")
        return await self.object_reader(uri)

    async def write_object(self, key: str, data: bytes) -> str:
        if self.object_writer is None:
            raise CapabilityError("object storage is not available on this host")
        return await self.object_writer(key, data)

    def complete_text(self, system: str, user: str) -> str:
        """Best-effort LLM extraction; returns "" when no LLM is available."""
        if self.text_completer is None:
            return ""
        return self.text_completer(system, user)

    async def open_signal(self, message: str, expects: str = "text") -> str:
        """Open a human-in-the-loop prompt and await the response. Raises
        CapabilityError if no HITL channel is wired on this host."""
        if self.signal_opener is None:
            raise CapabilityError("no human-in-the-loop channel on this host")
        return await self.signal_opener(message, expects)

    def complete_plan(self, messages: list[LLMMessage]) -> str:
        """Free-text completion via the planner seam; returns "" when no LLM is
        available. ``web_login`` uses this for selector disambiguation and parses
        JSON out of the returned text. Sync to match ``complete_text``; callers
        on the event loop wrap it in a thread (the agent proxy bridges back to
        the loop). The agent never calls OpenAI directly — this round-trips to
        the server, which owns the key."""
        if self.planner_completer is None:
            return ""
        return self.planner_completer(messages)
