"""The rendezvous relay.

Both sides dial OUT to this process; neither needs a stable address of its own:

    agent  --ws-->  BROKER  <--ws--  API (master link)
    (/ws/agents)              (/ws/master, X-Broker-Token)

The agent speaks its normal protocol — it just points AAKAAR_AGENT_SERVER at
the broker instead of the API. The broker allocates a session id per agent
socket, announces it up the master link, and from then on relays text frames
verbatim in both directions, multiplexed by session id. It never parses agent
frames and never verifies agent credentials: the ``x-agent-key`` header is
forwarded opaquely in the ``open`` envelope and the API performs the
authoritative DB check, exactly as for a direct connection. The key MUST never
be logged here.

Trust note: this process necessarily handles the key in cleartext, so the
broker host is trusted infrastructure — a hostile operator could capture keys
or forge ``data`` frames on sessions it relays (the API still pins each session
to the DB-verified key's tenant). See the package README's trust model.

Master-link framing (one JSON object per text frame):

    broker -> master   {"t": "open",  "sid": str, "headers": {"x-agent-key": str}}
    broker -> master   {"t": "data",  "sid": str, "frame": str}   agent -> API
    broker -> master   {"t": "close", "sid": str}                 agent went away
    master -> broker   {"t": "data",  "sid": str, "frame": str}   API -> agent
    master -> broker   {"t": "close", "sid": str}                 API drops the agent

Close codes used toward clients: 4401 bad broker token, 4408 handshake
timeout, 1013 try again later (no master online / session limit), 1012
service restart (master link lost or replaced), 1003 binary frame, 1009
frame too large after envelope escaping.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import uuid
from collections.abc import Coroutine, Mapping
from dataclasses import dataclass
from typing import Any

import websockets
from websockets.asyncio.server import Server, ServerConnection

logger = logging.getLogger("aakaar.broker")

MASTER_PATH = "/ws/master"
AGENT_PATH = "/ws/agents"  # same path the agent uses against the API directly

# Direct agent<->API frames are capped at 16 MiB (agent client / uvicorn). The
# master link wraps each frame in a JSON envelope whose string escaping can at
# worst double the size, so the broker accepts a bit over twice that.
MAX_FRAME_BYTES = 2 * 16 * 1024 * 1024 + 4096
_PING_INTERVAL = 20
_PING_TIMEOUT = 20
# Per-agent outbound buffer. The master read loop enqueues here instead of
# sending inline, so one non-draining agent socket can never stall dispatch to
# the rest of the fleet (head-of-line blocking). An agent that lets this fill
# is dropped rather than buffered without bound.
_DOWNLINK_QUEUE_MAX = 1024

_CLOSE = object()  # downlink-queue sentinel: drop this agent after pending data


@dataclass(slots=True)
class BrokerSettings:
    token: str
    host: str = "127.0.0.1"
    port: int = 9300
    max_sessions: int = 200
    handshake_timeout: float = 10.0


def load_broker_settings(env: Mapping[str, str] | None = None) -> BrokerSettings:
    """Build BrokerSettings from the environment.

    Required: AAKAAR_BROKER_TOKEN — there is deliberately no default. A relay
    with a guessable token would let anyone impersonate the API and receive
    agent sessions (and their keys), so the process refuses to start instead.
    """
    env = os.environ if env is None else env
    token = env.get("AAKAAR_BROKER_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "AAKAAR_BROKER_TOKEN is not set; refusing to start. Generate one with: "
            "python -c 'import secrets; print(secrets.token_urlsafe(32))' and set the "
            "same value on the API (AAKAAR_BROKER_TOKEN)."
        )
    return BrokerSettings(
        token=token,
        host=env.get("AAKAAR_BROKER_HOST", "127.0.0.1"),
        port=int(env.get("AAKAAR_BROKER_PORT", "9300")),
        max_sessions=int(env.get("AAKAAR_BROKER_MAX_SESSIONS", "200")),
        handshake_timeout=float(env.get("AAKAAR_BROKER_HANDSHAKE_TIMEOUT", "10")),
    )


class _AgentDownlink:
    """The master->agent side of one session: a bounded queue drained by its
    own task. Per-session isolation is what keeps a single non-draining agent
    from blocking the shared master read loop (and thus the whole fleet)."""

    def __init__(self, ws: ServerConnection) -> None:
        self.ws = ws
        self.queue: asyncio.Queue[str | object] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None

    async def drain(self) -> None:
        # Sends to a slow/dead peer block HERE, on this session's own task —
        # never on the master read loop. _quiet_send swallows peer errors so a
        # dead socket just ends the loop on the next ConnectionClosed.
        while True:
            item = await self.queue.get()
            if item is _CLOSE:
                await _quiet_close(self.ws, code=1000, reason="closed by server")
                return
            await _quiet_send(self.ws, item)  # type: ignore[arg-type]

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()


class RendezvousBroker:
    """One master link + up to ``max_sessions`` multiplexed agent sessions."""

    def __init__(self, settings: BrokerSettings) -> None:
        if not settings.token:
            raise ValueError("broker token must be non-empty")
        self._settings = settings
        self._server: Server | None = None
        self._master: ServerConnection | None = None
        self._agents: dict[str, ServerConnection] = {}
        # Per-session master->agent buffer + drainer (see _AgentDownlink); the
        # down path goes through these so a stalled agent can't block the fleet.
        self._downlinks: dict[str, _AgentDownlink] = {}
        # Session ids the master has answered (any frame routed down). Agent
        # sockets that never reach this state are dropped by the watchdog.
        self._paired: set[str] = set()
        self._watchdogs: set[asyncio.Task[None]] = set()

    @property
    def port(self) -> int:
        assert self._server is not None, "broker not started"
        return int(self._server.sockets[0].getsockname()[1])

    @property
    def session_count(self) -> int:
        return len(self._agents)

    async def start(self) -> None:
        self._server = await websockets.serve(
            self._route,
            self._settings.host,
            self._settings.port,
            max_size=MAX_FRAME_BYTES,
            ping_interval=_PING_INTERVAL,
            ping_timeout=_PING_TIMEOUT,
        )
        logger.info(
            "rendezvous broker listening on ws://%s:%d (max_sessions=%d)",
            self._settings.host,
            self.port,
            self._settings.max_sessions,
        )

    async def stop(self) -> None:
        for task in list(self._watchdogs):
            task.cancel()
        for link in list(self._downlinks.values()):
            link.cancel()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def serve_forever(self) -> None:
        assert self._server is not None, "broker not started"
        await self._server.wait_closed()

    # ---------- routing -----------------------------------------------------

    async def _route(self, ws: ServerConnection) -> None:
        path = ws.request.path.split("?", 1)[0].rstrip("/") if ws.request else ""
        if path == MASTER_PATH:
            await self._handle_master(ws)
        elif path == AGENT_PATH:
            await self._handle_agent(ws)
        else:
            await ws.close(code=1008, reason="unknown path")

    # ---------- master link ---------------------------------------------------

    async def _handle_master(self, ws: ServerConnection) -> None:
        presented = ws.request.headers.get("X-Broker-Token", "") if ws.request else ""
        if not hmac.compare_digest(presented.encode(), self._settings.token.encode()):
            logger.warning("master link rejected: bad token")
            await ws.close(code=4401, reason="bad broker token")
            return
        if self._master is not None:
            # A valid newcomer wins — this is how the API resumes after a
            # restart while the old TCP connection is still half-open.
            logger.warning("master link replaced by a new connection")
            previous, self._master = self._master, ws
            await self._close_agents(reason="master link replaced")
            await _quiet_close(previous, code=1012, reason="replaced by a newer master link")
        else:
            self._master = ws
        logger.info("master link established (%d live agent session(s))", len(self._agents))
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue  # protocol is text-only; drop stray binary frames
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                self._route_to_agent(msg)
        except websockets.ConnectionClosed:
            pass
        finally:
            if self._master is ws:
                self._master = None
                await self._close_agents(reason="master link lost")
                logger.info("master link lost")

    def _route_to_agent(self, msg: dict[str, Any]) -> None:
        # Synchronous on purpose: this runs on the shared master read loop, so
        # it must NEVER await a per-agent send (that is the head-of-line bug).
        # It only hands work to the target session's own bounded downlink.
        sid = msg.get("sid")
        if not isinstance(sid, str):
            return
        link = self._downlinks.get(sid)
        if link is None:
            return  # races with agent disconnect; nothing to do
        kind = msg.get("t")
        if kind == "data":
            self._paired.add(sid)
            frame = msg.get("frame")
            if not isinstance(frame, str):
                return
            if link.queue.qsize() >= _DOWNLINK_QUEUE_MAX:
                # A non-draining agent: drop it rather than buffer without
                # bound. Closing its socket ends _handle_agent, which cleans up.
                logger.warning("agent session sid=%s downlink flooded; dropping it", sid)
                self._spawn_close(link.ws, code=1013, reason="downlink overflow")
                return
            link.queue.put_nowait(frame)
        elif kind == "close":
            link.queue.put_nowait(_CLOSE)

    # ---------- agent sessions ------------------------------------------------

    async def _handle_agent(self, ws: ServerConnection) -> None:
        master = self._master
        if master is None:
            await ws.close(code=1013, reason="no master online")
            return
        if len(self._agents) >= self._settings.max_sessions:
            logger.warning("agent refused: session limit %d reached", self._settings.max_sessions)
            await ws.close(code=1013, reason="session limit reached")
            return
        sid = uuid.uuid4().hex
        self._agents[sid] = ws
        link = _AgentDownlink(ws)
        link.task = asyncio.ensure_future(link.drain())
        self._downlinks[sid] = link
        self._spawn_watchdog(sid)
        # Forward ONLY the agent's credential header, opaquely. Never log it.
        key = ws.request.headers.get("X-Agent-Key") if ws.request else None
        headers = {"x-agent-key": key} if key else {}
        logger.info("agent session open sid=%s (%d live)", sid, len(self._agents))
        try:
            await master.send(json.dumps({"t": "open", "sid": sid, "headers": headers}))
            async for frame in ws:
                if isinstance(frame, bytes):
                    await ws.close(code=1003, reason="text frames only")
                    break
                up = self._master
                if up is None:
                    break
                envelope = json.dumps({"t": "data", "sid": sid, "frame": frame})
                if len(envelope) > MAX_FRAME_BYTES:
                    # JSON escaping can inflate a frame past the master link's
                    # receive cap; sending it would kill the shared link (and
                    # every session with it). Drop only the offending agent.
                    await ws.close(code=1009, reason="frame too large")
                    break
                await up.send(envelope)
        except websockets.ConnectionClosed:
            pass
        finally:
            # _close_agents (master lost/replaced) evicts sessions first; in
            # that case the sid means nothing to the current master link, so
            # only sessions we evict ourselves get a close notification.
            was_live = self._agents.pop(sid, None) is not None
            gone = self._downlinks.pop(sid, None)
            if gone is not None:
                gone.cancel()
            self._paired.discard(sid)
            up = self._master
            if was_live and up is not None:
                await _quiet_send(up, json.dumps({"t": "close", "sid": sid}))
            logger.info("agent session closed sid=%s (%d live)", sid, len(self._agents))

    def _spawn_watchdog(self, sid: str) -> None:
        async def watchdog() -> None:
            await asyncio.sleep(self._settings.handshake_timeout)
            if sid in self._paired:
                return
            agent = self._agents.get(sid)
            if agent is not None:
                logger.info("agent session sid=%s dropped: handshake timeout", sid)
                await _quiet_close(agent, code=4408, reason="handshake timeout")

        self._track(watchdog())

    def _track(self, coro: Coroutine[Any, Any, None]) -> None:
        # The loop holds only weak refs to tasks; keep one until completion.
        task = asyncio.ensure_future(coro)
        self._watchdogs.add(task)
        task.add_done_callback(self._watchdogs.discard)

    def _spawn_close(self, ws: ServerConnection, *, code: int, reason: str) -> None:
        # Closing a flooded agent must not block the master read loop either.
        self._track(_quiet_close(ws, code=code, reason=reason))

    async def _close_agents(self, *, reason: str) -> None:
        agents = list(self._agents.values())
        self._agents.clear()
        self._paired.clear()
        for link in self._downlinks.values():
            link.cancel()  # stop drainers; _handle_agent finally pops the entry
        if not agents:
            return
        logger.info("closing %d agent session(s): %s", len(agents), reason)
        await asyncio.gather(
            *(_quiet_close(a, code=1012, reason=reason) for a in agents),
            return_exceptions=True,
        )


async def _quiet_send(ws: ServerConnection, frame: str) -> None:
    # A peer that died mid-relay must not tear down the caller's read loop.
    with contextlib.suppress(Exception):
        await ws.send(frame)


async def _quiet_close(ws: ServerConnection, *, code: int, reason: str) -> None:
    with contextlib.suppress(Exception):
        await ws.close(code=code, reason=reason)


__all__ = [
    "AGENT_PATH",
    "MASTER_PATH",
    "MAX_FRAME_BYTES",
    "BrokerSettings",
    "RendezvousBroker",
    "load_broker_settings",
]
