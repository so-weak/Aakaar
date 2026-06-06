"""Wire contract between the server and a remote agent.

Transport is JSON over an authenticated WebSocket (agent dials the server).
Message shapes (``type`` discriminator):

  agent -> server  hello      {type, alias, os, gui, version, capabilities:[{ref,version}]}
  server -> agent  task       {type, task_id, run_id, node_id, ref, inputs, secrets, timeout_s}
  agent -> server  result     {type, task_id, ok, outputs?, error?}
  agent -> server  event      {type, run_id, node_id, kind, payload}   (optional progress)
  both             ping/pong  WebSocket frames (liveness)

``secrets`` is the credential envelope: only the values a node needs, fetched
just-in-time by the server from the vault and never persisted by the agent.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class AgentCapability:
    ref: str
    version: str = "1"


@dataclass
class AgentInfo:
    alias: str
    tenant_id: uuid.UUID
    os: str = "unknown"
    gui_capable: bool = False
    version: str = "0"
    hostname: str | None = None
    pools: tuple[str, ...] = ()
    capabilities: tuple[AgentCapability, ...] = ()

    def supports(self, ref: str, version: str | None = None) -> bool:
        for c in self.capabilities:
            if c.ref == ref and (version is None or c.version == version):
                return True
        return False

    def in_pool(self, label: str) -> bool:
        return label in self.pools


@dataclass
class RemoteTask:
    task_id: str
    run_id: str
    node_id: str
    ref: str
    inputs: dict[str, Any]
    secrets: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 300.0

    def to_wire(self) -> dict[str, Any]:
        return {
            "type": "task",
            "task_id": self.task_id,
            "run_id": self.run_id,
            "node_id": self.node_id,
            "ref": self.ref,
            "inputs": self.inputs,
            "secrets": self.secrets,
            "timeout_s": self.timeout_s,
        }


@dataclass
class RemoteResult:
    task_id: str
    ok: bool
    outputs: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None

    @classmethod
    def from_wire(cls, msg: dict[str, Any]) -> RemoteResult:
        return cls(
            task_id=str(msg.get("task_id", "")),
            ok=bool(msg.get("ok")),
            outputs=msg.get("outputs") or {},
            error=msg.get("error"),
        )


def new_task_id() -> str:
    return uuid.uuid4().hex


def parse_hello(msg: dict[str, Any], *, alias: str, tenant_id: uuid.UUID) -> AgentInfo:
    caps = tuple(
        AgentCapability(ref=str(c.get("ref")), version=str(c.get("version", "1")))
        for c in (msg.get("capabilities") or [])
        if c.get("ref")
    )
    pools = tuple(str(p) for p in (msg.get("pools") or []))
    return AgentInfo(
        alias=alias,
        tenant_id=tenant_id,
        os=str(msg.get("os", "unknown")),
        gui_capable=bool(msg.get("gui")),
        version=str(msg.get("version", "0")),
        hostname=msg.get("hostname"),
        pools=pools,
        capabilities=caps,
    )


@runtime_checkable
class AgentConnection(Protocol):
    """A live connection to one remote agent."""

    @property
    def info(self) -> AgentInfo: ...

    async def dispatch(self, task: RemoteTask) -> RemoteResult: ...

    async def close(self) -> None: ...
