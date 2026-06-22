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
    public_key: str | None = None
    """Agent's sealed-box public key (hex), from its hello. The server seals the
    secrets envelope + obj_get replies to it so the broker relays ciphertext."""

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
    tenant_id: str = ""
    """The run's tenant. The agent keys per-run session state by it and the
    server stamps it from the authenticated identity on obj/llm back-channel
    requests — never trusting an agent-supplied tenant."""
    secrets_sealed: dict[str, Any] | None = None
    """When set, the credential envelope sealed to the agent's public key; the
    cleartext ``secrets`` field is then empty. The agent unseals it locally."""
    live_screen: bool = False
    """Ask the agent to emit a live-preview screenshot event after this node
    (set for browser nodes when the server's live_screenshots setting is on)."""

    def to_wire(self) -> dict[str, Any]:
        return {
            "type": "task",
            "task_id": self.task_id,
            "run_id": self.run_id,
            "node_id": self.node_id,
            "tenant_id": self.tenant_id,
            "ref": self.ref,
            "inputs": self.inputs,
            "secrets": self.secrets,
            "secrets_sealed": self.secrets_sealed,
            "live_screen": self.live_screen,
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


def new_request_id() -> str:
    """Correlation id for a back-channel request/response exchange — distinct
    from a task_id (which correlates a node dispatch + its result). Used by both
    directions of the back-channel:
      agent -> server  req  {type:"req",  request_id, op, ...}  -> reply {type:"reply", request_id, ok, result|error}
      server -> agent  ctrl {type:"ctrl", request_id, op, ...}  <- ack   {type:"ack",   request_id, ok, error?}
    """
    return uuid.uuid4().hex


def parse_hello(msg: dict[str, Any], *, alias: str, tenant_id: uuid.UUID) -> AgentInfo:
    caps = tuple(
        AgentCapability(ref=str(c.get("ref")), version=str(c.get("version", "1")))
        for c in (msg.get("capabilities") or [])
        if c.get("ref")
    )
    pools = tuple(str(p) for p in (msg.get("pools") or []))
    pubkey = msg.get("pubkey")
    return AgentInfo(
        alias=alias,
        tenant_id=tenant_id,
        os=str(msg.get("os", "unknown")),
        gui_capable=bool(msg.get("gui")),
        version=str(msg.get("version", "0")),
        hostname=msg.get("hostname"),
        pools=pools,
        capabilities=caps,
        public_key=str(pubkey) if pubkey else None,
    )


@runtime_checkable
class AgentConnection(Protocol):
    """A live connection to one remote agent."""

    @property
    def info(self) -> AgentInfo: ...

    async def dispatch(self, task: RemoteTask) -> RemoteResult: ...

    async def close(self) -> None: ...
