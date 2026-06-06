"""Remote execution: dispatch capability nodes to tenant-scoped agents.

The server stays the orchestrator; an agent is a capability executor that runs
nodes whose DAG ``target`` selects it. Pieces:
  - protocol:   wire contract (tasks, results, events, hello) + AgentConnection
  - registry:   in-memory live-connection registry + placement resolver
  - dispatcher: resolve an agent, send a task (with a credential envelope),
                await the result under a deadline, audit which agent ran it
  - connection: WebSocket-backed AgentConnection (+ a fake for tests)
"""

from aakaar.workers.remote.connection import FakeAgentConnection, WebSocketAgentConnection
from aakaar.workers.remote.dispatcher import RemoteDispatcher, RemoteExecError
from aakaar.workers.remote.placement import check_placement
from aakaar.workers.remote.protocol import (
    AgentCapability,
    AgentConnection,
    AgentInfo,
    RemoteResult,
    RemoteTask,
    parse_hello,
)
from aakaar.workers.remote.registry import AgentRegistry, NoAgentAvailable

__all__ = [
    "AgentCapability",
    "AgentConnection",
    "AgentInfo",
    "AgentRegistry",
    "FakeAgentConnection",
    "NoAgentAvailable",
    "RemoteDispatcher",
    "RemoteExecError",
    "RemoteResult",
    "RemoteTask",
    "WebSocketAgentConnection",
    "check_placement",
    "parse_hello",
]
