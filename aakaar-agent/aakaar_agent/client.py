"""Agent connection loop.

Dials OUT to the server's ``/ws/agents`` endpoint (so the workstation needs no
inbound ports), authenticates with its enrollment key, announces its OS / GUI
session / capabilities, then serves dispatched tasks until the socket drops.
Tasks run concurrently so a slow one doesn't block the channel; each reply is
correlated by ``task_id``.

Robustness semantics:

- Reconnects with exponential backoff plus jitter (base ``reconnect_delay``,
  doubling per failed attempt, capped at ``_BACKOFF_CAP``); a connection that
  stays up for ``_BACKOFF_RESET_S`` resets the backoff.
- Half-dead TCP links are detected by the websocket-level keepalive
  (``ping_interval``/``ping_timeout``): a peer that stops answering pings gets
  the connection torn down, which re-enters the reconnect loop.
- In-flight tasks keep running across a disconnect. Results are RE-DELIVERED,
  never re-executed: every completed reply is kept in a bounded LRU cache, a
  reply that could not be sent is flushed right after the next successful
  hello, and a server redispatch of a known ``task_id`` (in flight or cached)
  is answered from the cache instead of running the capability again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import socket
import time
from collections import OrderedDict

import websockets

from aakaar_agent import VERSION
from aakaar_agent.capabilities import advertised, dispatch, load_capabilities
from aakaar_agent.session import detect_gui, detect_os

logger = logging.getLogger(__name__)

_BACKOFF_CAP = 60.0
_BACKOFF_RESET_S = 30.0  # connection uptime that counts as "healthy again"
_PING_INTERVAL = 20
_PING_TIMEOUT = 10
_OPEN_TIMEOUT = 15
_RESULT_CACHE_MAX = 128


class AgentClient:
    def __init__(self, ws_url: str, agent_key: str, *, reconnect_delay: float = 1.0) -> None:
        self._url = ws_url
        self._key = agent_key
        self._delay = reconnect_delay  # backoff base
        self._attempts = 0
        self._stop = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None  # set by run_forever
        self._ws: websockets.ClientConnection | None = None
        self._inflight: dict[str, asyncio.Task] = {}
        self._results: OrderedDict[str, dict] = OrderedDict()  # task_id -> reply
        self._undelivered: set[str] = set()  # task_ids whose reply send failed
        self._aux_tasks: set[asyncio.Task] = set()  # strong refs, see _spawn

    def stop(self) -> None:
        """Request graceful shutdown; safe to call from any thread.

        ``_stop`` is an :class:`asyncio.Event` owned by run_forever()'s loop, so
        its waiters can only be woken there. When called from outside that loop
        (e.g. a signal handler or service-manager thread) we hop onto the loop
        via ``call_soon_threadsafe`` to set the event AND close the live socket;
        otherwise run_forever() would stay parked in ``async for raw in ws``
        until the server dropped the connection.
        """
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop or loop is None:
            # In-loop (or never started): set + close directly.
            self._do_stop()
        else:
            loop.call_soon_threadsafe(self._do_stop)

    def _do_stop(self) -> None:
        """Set the stop event and close the live socket. Runs on the loop."""
        self._stop.set()
        ws = self._ws
        if ws is not None:
            self._spawn(ws.close())

    async def run_forever(self) -> None:
        self._loop = asyncio.get_running_loop()
        load_capabilities()
        while not self._stop.is_set():
            connected_at = time.monotonic()
            try:
                await self._connect_once()
            except Exception as e:
                logger.warning("agent connection lost: %s", e)
            if self._stop.is_set():
                break
            if time.monotonic() - connected_at >= _BACKOFF_RESET_S:
                self._attempts = 0
            delay = self._next_delay()
            logger.info("reconnecting in %.1fs", delay)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    def _next_delay(self) -> float:
        delay = min(_BACKOFF_CAP, self._delay * (2 ** min(self._attempts, 16)))
        self._attempts += 1
        return delay * random.uniform(0.5, 1.0)  # jitter, de-syncs agent herds

    def _hello(self) -> dict:
        return {
            "type": "hello",
            "os": detect_os(),
            "gui": detect_gui(),
            "version": VERSION,
            "hostname": socket.gethostname(),
            "capabilities": advertised(),
        }

    async def _connect_once(self) -> None:
        async with websockets.connect(
            self._url,
            additional_headers={"X-Agent-Key": self._key},
            open_timeout=_OPEN_TIMEOUT,
            ping_interval=_PING_INTERVAL,
            ping_timeout=_PING_TIMEOUT,
            max_size=16 * 1024 * 1024,
        ) as ws:
            self._ws = ws
            # stop() may have fired while the dial was in flight: it saw _ws as
            # None and had nothing to close. Re-check now that the socket is live
            # so we don't send hello and park in the read loop on a connection
            # we have already been told to abandon (the context manager closes
            # ws on return).
            if self._stop.is_set():
                self._ws = None
                return
            await ws.send(json.dumps(self._hello()))
            logger.info("agent connected to %s", self._url)
            try:
                await self._flush_undelivered()
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if msg.get("type") == "task":
                        self._handle_task(msg)
            finally:
                self._ws = None

    async def _flush_undelivered(self) -> None:
        for task_id in list(self._undelivered):
            reply = self._results.get(task_id)
            self._undelivered.discard(task_id)
            if reply is not None:
                await self._send_reply(reply)

    def _spawn(self, coro) -> asyncio.Task:
        # The loop only keeps weak refs to tasks; hold one until completion.
        task = asyncio.create_task(coro)
        self._aux_tasks.add(task)
        task.add_done_callback(self._aux_tasks.discard)
        return task

    def _handle_task(self, msg: dict) -> None:
        task_id = msg.get("task_id")
        if task_id:
            cached = self._results.get(task_id)
            if cached is not None:  # redispatch of a finished task: re-deliver
                self._spawn(self._send_reply(cached))
                return
            if task_id in self._inflight:  # already executing: one reply later
                return
        task = self._spawn(self._run_and_reply(msg))
        if task_id:
            self._inflight[task_id] = task
            task.add_done_callback(lambda _t: self._inflight.pop(task_id, None))

    async def _run_and_reply(self, msg: dict) -> None:
        task_id = msg.get("task_id")
        ref = str(msg.get("ref") or "")  # unknown refs fail in dispatch()
        try:
            outputs = await dispatch(ref, msg.get("inputs") or {}, msg.get("secrets") or {})
            reply = {"type": "result", "task_id": task_id, "ok": True, "outputs": outputs}
        except Exception as e:
            logger.warning("task %s (%s) failed: %s", task_id, ref, e)
            reply = {
                "type": "result",
                "task_id": task_id,
                "ok": False,
                "error": {"type": type(e).__name__, "message": str(e)[:500]},
            }
        if task_id:
            self._results[task_id] = reply
            self._results.move_to_end(task_id)
            while len(self._results) > _RESULT_CACHE_MAX:
                evicted, _ = self._results.popitem(last=False)
                self._undelivered.discard(evicted)
        await self._send_reply(reply)

    async def _send_reply(self, reply: dict) -> None:
        ws = self._ws
        try:
            if ws is None:
                raise ConnectionError("not connected")
            await ws.send(json.dumps(reply))
        except Exception:
            task_id = reply.get("task_id")
            if task_id:
                self._undelivered.add(task_id)
            logger.warning(
                "result for task %s not sent; will re-deliver after reconnect",
                task_id,
            )
