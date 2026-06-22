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
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aakaar.workers.remote.protocol import (
    AgentInfo,
    RemoteResult,
    RemoteTask,
    new_request_id,
)

logger = logging.getLogger(__name__)


class WebSocketAgentConnection:
    def __init__(self, websocket: Any, info: AgentInfo) -> None:
        self._ws = websocket
        self._info = info
        self._pending: dict[str, asyncio.Future[RemoteResult]] = {}
        # Server-initiated control requests (run_end/cancel) awaiting the agent's
        # ack, correlated by request_id. Independent of the task `_pending` map.
        self._ctrl_pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

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

    # ---- back-channel: server -> agent requests + agent -> server replies ----

    async def _send(self, frame: dict[str, Any]) -> None:
        await self._ws.send_json(frame)

    async def send_reply(
        self,
        request_id: str | None,
        *,
        ok: bool,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """Reply to an agent-initiated `req` (obj_get / signal_open / llm_*)."""
        if not request_id:
            return
        await self._send(
            {"type": "reply", "request_id": request_id, "ok": ok, "result": result or {}, "error": error}
        )

    async def request(
        self, op: str, payload: dict[str, Any] | None = None, *, timeout: float = 300.0
    ) -> dict[str, Any]:
        """Server-initiated request to the agent (e.g. run_end, cancel). Awaits
        the agent's ack. Raises ConnectionError if the socket drops first."""
        loop = asyncio.get_running_loop()
        rid = new_request_id()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._ctrl_pending[rid] = fut
        try:
            await self._send({"type": "ctrl", "request_id": rid, "op": op, **(payload or {})})
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._ctrl_pending.pop(rid, None)

    async def notify(self, op: str, payload: dict[str, Any] | None = None) -> None:
        """Fire-and-forget control frame (no ack awaited). Best-effort — a dead
        socket is swallowed so cleanup paths never raise."""
        with contextlib.suppress(Exception):
            await self._send({"type": "ctrl", "request_id": new_request_id(), "op": op, **(payload or {})})

    def resolve_ack(self, msg: dict[str, Any]) -> None:
        """Called by the demux when an agent `ack` frame arrives."""
        fut = self._ctrl_pending.get(str(msg.get("request_id", "")))
        if fut is not None and not fut.done():
            fut.set_result(msg)

    def fail_pending(self, reason: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError(reason))
        self._pending.clear()
        for fut in self._ctrl_pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError(reason))
        self._ctrl_pending.clear()

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
        if inspect.isawaitable(res):
            return await res
        return res

    async def close(self) -> None:  # pragma: no cover - trivial
        pass


__all__ = ["FakeAgentConnection", "WebSocketAgentConnection"]
