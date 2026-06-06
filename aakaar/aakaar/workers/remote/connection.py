"""AgentConnection implementations.

`WebSocketAgentConnection` wraps a live agent WebSocket: ``dispatch`` sends a
task and awaits a Future that the router's read-loop resolves when the matching
result arrives (correlated by ``task_id``). `FakeAgentConnection` runs a handler
in-process for tests, so the dispatcher/executor can be exercised end-to-end
without a real agent or socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aakaar.workers.remote.protocol import AgentInfo, RemoteResult, RemoteTask

logger = logging.getLogger(__name__)


class WebSocketAgentConnection:
    def __init__(self, websocket: Any, info: AgentInfo) -> None:
        self._ws = websocket
        self._info = info
        self._pending: dict[str, asyncio.Future[RemoteResult]] = {}

    @property
    def info(self) -> AgentInfo:
        return self._info

    async def dispatch(self, task: RemoteTask) -> RemoteResult:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[RemoteResult] = loop.create_future()
        self._pending[task.task_id] = fut
        try:
            await self._ws.send_json(task.to_wire())
            return await fut
        finally:
            self._pending.pop(task.task_id, None)

    def resolve_result(self, msg: dict[str, Any]) -> None:
        """Called by the router read-loop when a result frame arrives."""
        fut = self._pending.get(str(msg.get("task_id", "")))
        if fut is not None and not fut.done():
            fut.set_result(RemoteResult.from_wire(msg))

    def fail_pending(self, reason: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError(reason))
        self._pending.clear()

    async def close(self) -> None:
        self.fail_pending("agent connection closed")
        with contextlib.suppress(Exception):  # pragma: no cover - best-effort
            await self._ws.close()


FakeHandler = Callable[[RemoteTask], "RemoteResult | Awaitable[RemoteResult]"]


class FakeAgentConnection:
    """In-process AgentConnection for tests."""

    def __init__(self, info: AgentInfo, handler: FakeHandler) -> None:
        self._info = info
        self._handler = handler
        self.dispatched: list[RemoteTask] = []

    @property
    def info(self) -> AgentInfo:
        return self._info

    async def dispatch(self, task: RemoteTask) -> RemoteResult:
        self.dispatched.append(task)
        res = self._handler(task)
        if asyncio.iscoroutine(res):
            res = await res
        return res

    async def close(self) -> None:  # pragma: no cover - trivial
        pass


__all__ = ["FakeAgentConnection", "WebSocketAgentConnection"]
