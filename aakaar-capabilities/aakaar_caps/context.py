"""The portable context a shared capability is handed.

Only this surface is guaranteed across hosts. ``secrets`` is always present
(empty if the capability needs none). Object storage and the LLM are optional —
the server provides them; a remote agent may not, in which case using them
raises ``CapabilityError`` (so a capability that needs them simply can't run on
a host that lacks them, which placement should already prevent).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field


class CapabilityError(RuntimeError):
    """A capability could not run on this host (e.g. a service it needs is absent)."""


@dataclass
class CapabilityContext:
    secrets: dict[str, str] = field(default_factory=dict)
    tenant_id: str | None = None
    run_id: str | None = None
    object_reader: Callable[[str], Awaitable[bytes]] | None = None
    object_writer: Callable[[str, bytes], Awaitable[str]] | None = None
    text_completer: Callable[[str, str], str] | None = None

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
