"""Agent connection loop.

Dials OUT to the server's ``/ws/agents`` endpoint (so the workstation needs no
inbound ports), authenticates with its enrollment key, announces its OS / GUI
session / capabilities, then serves dispatched tasks until the socket drops —
reconnecting with a fixed delay. Tasks run concurrently so a slow one doesn't
block the channel; each reply is correlated by ``task_id``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket

import websockets

from aakaar_agent import VERSION
from aakaar_agent.capabilities import advertised, dispatch, load_capabilities
from aakaar_agent.session import detect_gui, detect_os

logger = logging.getLogger(__name__)


class AgentClient:
    def __init__(self, ws_url: str, agent_key: str, *, reconnect_delay: float = 5.0) -> None:
        self._url = ws_url
        self._key = agent_key
        self._delay = reconnect_delay
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        load_capabilities()
        while not self._stop.is_set():
            try:
                await self._connect_once()
            except Exception as e:
                logger.warning("agent connection lost: %s", e)
            if self._stop.is_set():
                break
            await asyncio.sleep(self._delay)

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
            ping_interval=20,
            ping_timeout=10,
            max_size=16 * 1024 * 1024,
        ) as ws:
            await ws.send(json.dumps(self._hello()))
            logger.info("agent connected to %s", self._url)
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if msg.get("type") == "task":
                    asyncio.create_task(self._run_and_reply(ws, msg))

    async def _run_and_reply(self, ws: object, msg: dict) -> None:
        task_id = msg.get("task_id")
        ref = msg.get("ref")
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
        try:
            await ws.send(json.dumps(reply))  # type: ignore[attr-defined]
        except Exception:
            logger.warning("could not send result for task %s", task_id)
