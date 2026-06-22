"""Master link to a rendezvous broker (see the aakaar-broker package).

When neither the API nor its agents have a stable address, both dial OUT to
the broker; the broker pairs each agent socket onto this single master link
and relays frames blindly, multiplexed by session id:

    agent  --ws-->  broker  <--ws--  BrokerLink (this module, in the API)

Every relayed session is authenticated HERE, never by the broker: the agent's
``x-agent-key`` header travels end-to-end inside the broker's ``open``
envelope and is verified against the same DB rows as a direct ``/ws/agents``
connection (`authenticate_agent_key`, the shared helper for both paths). A
verified session then goes through the identical hello -> ``parse_hello`` ->
``AgentRegistry.register`` flow, so the dispatcher cannot tell relayed agents
from direct ones.

Master-link framing (one JSON object per text frame):

    broker -> us   {"t": "open",  "sid": str, "headers": {"x-agent-key": str}}
    broker -> us   {"t": "data",  "sid": str, "frame": str}   agent -> API
    broker -> us   {"t": "close", "sid": str}                 agent went away
    us -> broker   {"t": "data",  "sid": str, "frame": str}   API -> agent
    us -> broker   {"t": "close", "sid": str}                 drop the agent

The link reconnects with exponential backoff + jitter; on every drop the
broker closes its agent sockets (they re-dial), so sessions never have to be
resumed — only re-established.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aakaar.db.session import SessionFactory
from aakaar.workers.remote.backchannel import demux_agent_frame
from aakaar.workers.remote.connection import WebSocketAgentConnection
from aakaar.workers.remote.protocol import parse_hello
from aakaar.workers.remote.registry import AgentRegistry

logger = logging.getLogger(__name__)

_BACKOFF_CAP_S = 30.0
_BACKOFF_RESET_S = 30.0
_OPEN_TIMEOUT_S = 15.0
_PING_INTERVAL_S = 20.0
_PING_TIMEOUT_S = 10.0
_HELLO_TIMEOUT_S = 10.0
# Same sizing rationale as the broker: 16 MiB agent frames + envelope escaping.
_MAX_FRAME_BYTES = 2 * 16 * 1024 * 1024 + 4096
# A session whose handler stops draining (or a frame flood) is cut off rather
# than buffering without bound.
_SESSION_QUEUE_MAX = 1024

_EOF = None  # queue sentinel: the broker reported the agent session closed


# ---------- shared agent-key verification -----------------------------------


@dataclass(frozen=True)
class AgentIdentity:
    agent_id: uuid.UUID
    tenant_id: uuid.UUID
    alias: str
    pools: tuple[str, ...]


def parse_agent_key(raw: str | None) -> tuple[uuid.UUID, str] | None:
    """Split an enrollment key ``"<agent_id>.<secret>"``; None when malformed."""
    if not raw or "." not in raw:
        return None
    agent_id_str, _, secret = raw.partition(".")
    try:
        return uuid.UUID(agent_id_str), secret
    except ValueError:
        return None


def authenticate_agent_key(
    session_factory: SessionFactory, raw: str | None
) -> AgentIdentity | None:
    """Verify an agent enrollment key against the DB — the exact check the
    direct ``/ws/agents`` endpoint performs. Returns None on any failure; the
    key itself must never be logged."""
    # Imported here, not at module top: aakaar.api transitively imports this
    # package while loading, so api-layer imports must be deferred.
    from aakaar.api.auth.passwords import verify_password
    from aakaar.db.models import RemoteAgent

    parsed = parse_agent_key(raw)
    if parsed is None:
        return None
    agent_id, secret = parsed
    with session_factory.session() as s:
        agent = s.get(RemoteAgent, agent_id)
        if agent is None or not verify_password(secret, agent.api_key_hash):
            return None
        return AgentIdentity(
            agent_id=agent.id,
            tenant_id=agent.tenant_id,
            alias=agent.alias,
            pools=tuple(agent.pools or []),
        )


def master_link_url(base: str) -> str:
    """Normalize a broker base URL to its master WebSocket endpoint."""
    url = base.strip().rstrip("/")
    if url.startswith("http://"):
        url = "ws://" + url[len("http://") :]
    elif url.startswith("https://"):
        url = "wss://" + url[len("https://") :]
    return url + "/ws/master"


# ---------- the link ---------------------------------------------------------


class _AgentSession:
    """One relayed agent: an inbound frame queue drained by a handler task."""

    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None

    def feed(self, frame: str | None) -> None:
        self.queue.put_nowait(frame)

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()


class _SessionTransport:
    """Duck-types the WebSocket surface `WebSocketAgentConnection` uses
    (send_json/close), tunneling frames up the master link for one session."""

    def __init__(self, link: BrokerLink, sid: str) -> None:
        self._link = link
        self._sid = sid

    async def send_json(self, payload: dict[str, Any]) -> None:
        await self._link.send_frame(self._sid, json.dumps(payload))

    async def close(self) -> None:
        await self._link.send_session_close(self._sid)


class BrokerLink:
    def __init__(
        self,
        *,
        url: str,
        token: str,
        session_factory: SessionFactory,
        agent_registry: AgentRegistry,
        recorder: Any = None,
        request_handler: Any = None,
        server_pubkey: str | None = None,
        reconnect_delay: float = 1.0,
        hello_timeout: float = _HELLO_TIMEOUT_S,
    ) -> None:
        if not token:
            raise ValueError("broker link requires a non-empty token")
        self._url = master_link_url(url)
        self._token = token
        self._session_factory = session_factory
        self._registry = agent_registry
        self._recorder = recorder  # event recorder, same role as the direct path
        # Back-channel handler for agent-initiated requests; the relayed read
        # loop routes `req` frames here exactly like the direct /ws/agents path.
        self._request_handler = request_handler
        # Server sealed-box public key, advertised in welcome so the agent can
        # seal obj_put bodies to us over the broker.
        self._server_pubkey = server_pubkey
        self._delay = reconnect_delay  # backoff base
        self._hello_timeout = hello_timeout
        self._attempts = 0
        self._stop = asyncio.Event()
        self._ws: Any = None
        self._runner: asyncio.Task[None] | None = None
        self._sessions: dict[str, _AgentSession] = {}
        self._aux_tasks: set[asyncio.Task[None]] = set()  # strong refs

    async def start(self) -> None:
        self._stop.clear()
        self._runner = asyncio.create_task(self._run(), name="broker-master-link")

    async def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        if self._runner is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner
            self._runner = None

    @property
    def connected(self) -> bool:
        return self._ws is not None

    # ---------- connection loop ------------------------------------------------

    async def _run(self) -> None:
        # Lazy: websockets ships via uvicorn[standard]; only the broker path needs it.
        import websockets

        while not self._stop.is_set():
            connected_at = time.monotonic()
            try:
                await self._connect_once(websockets)
            except Exception as e:
                logger.warning("broker master link lost: %s", e)
            if self._stop.is_set():
                break
            if time.monotonic() - connected_at >= _BACKOFF_RESET_S:
                self._attempts = 0
            delay = self._next_delay()
            logger.info("broker master link reconnecting in %.1fs", delay)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)

    def _next_delay(self) -> float:
        delay = min(_BACKOFF_CAP_S, self._delay * (2.0 ** min(self._attempts, 16)))
        self._attempts += 1
        return delay * random.uniform(0.5, 1.0)

    async def _connect_once(self, websockets: Any) -> None:
        async with websockets.connect(
            self._url,
            additional_headers={"X-Broker-Token": self._token},
            open_timeout=_OPEN_TIMEOUT_S,
            ping_interval=_PING_INTERVAL_S,
            ping_timeout=_PING_TIMEOUT_S,
            max_size=_MAX_FRAME_BYTES,
        ) as ws:
            self._ws = ws
            # stop() may have run while the dial was in flight; it saw _ws as
            # None and had nothing to close, so check before settling into the
            # read loop or the link would outlive shutdown.
            if self._stop.is_set():
                self._ws = None
                return
            logger.info("broker master link established: %s", self._url)
            try:
                async for raw in ws:
                    if isinstance(raw, bytes):
                        continue  # protocol is text-only
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    self._handle_envelope(msg)
            finally:
                self._ws = None
                await self._drain_sessions()

    def _handle_envelope(self, msg: dict[str, Any]) -> None:
        kind = msg.get("t")
        sid = msg.get("sid")
        if not isinstance(sid, str) or not sid:
            return
        if kind == "open":
            headers = msg.get("headers")
            self._open_session(sid, headers if isinstance(headers, dict) else {})
            return
        sess = self._sessions.get(sid)
        if sess is None:
            return  # races with session teardown
        if kind == "data":
            frame = msg.get("frame")
            if not isinstance(frame, str):
                return
            if sess.queue.qsize() >= _SESSION_QUEUE_MAX:
                logger.warning("broker session %s flooded; dropping it", sid)
                sess.cancel()
                return
            sess.feed(frame)
        elif kind == "close":
            sess.feed(_EOF)

    # ---------- per-session lifecycle -------------------------------------------

    def _open_session(self, sid: str, headers: dict[str, Any]) -> None:
        if sid in self._sessions:  # the broker never reuses sids; defensive
            return
        sess = _AgentSession(sid)
        self._sessions[sid] = sess
        sess.task = asyncio.create_task(
            self._serve_session(sess, headers), name=f"broker-session-{sid}"
        )

        def _cleanup(_t: asyncio.Task[None]) -> None:
            if self._sessions.get(sid) is sess:
                del self._sessions[sid]

        sess.task.add_done_callback(_cleanup)

    async def _serve_session(self, sess: _AgentSession, headers: dict[str, Any]) -> None:
        # Whatever goes wrong (malformed hello, DB hiccup, flood-cancel), the
        # broker side of the session must be told to drop the agent socket —
        # otherwise it would dangle there until the agent gives up on its own.
        try:
            await self._pair_and_pump(sess, headers)
        except asyncio.CancelledError:
            self._spawn(self._send_session_close_quiet(sess.sid))
            raise
        except Exception:
            logger.warning("broker session %s failed", sess.sid, exc_info=True)
            self._spawn(self._send_session_close_quiet(sess.sid))

    async def _pair_and_pump(self, sess: _AgentSession, headers: dict[str, Any]) -> None:
        sid = sess.sid
        raw_key = headers.get("x-agent-key")
        raw_key = raw_key if isinstance(raw_key, str) else None
        # bcrypt verification is ~100ms of CPU; keep it off the event loop.
        identity = await asyncio.to_thread(
            authenticate_agent_key, self._session_factory, raw_key
        )
        if identity is None:
            logger.info("broker session %s rejected: agent key verification failed", sid)
            await self._send_session_close_quiet(sid)
            return

        try:
            first = await asyncio.wait_for(sess.queue.get(), timeout=self._hello_timeout)
        except TimeoutError:
            logger.info("broker session %s dropped: no hello", sid)
            await self._send_session_close_quiet(sid)
            return
        hello = self._parse_json(first)
        if hello is None:
            await self._send_session_close_quiet(sid)
            return
        try:
            info = parse_hello(hello, alias=identity.alias, tenant_id=identity.tenant_id)
        except Exception:
            logger.info("broker session %s dropped: malformed hello", sid)
            await self._send_session_close_quiet(sid)
            return
        info.pools = identity.pools  # admin-controlled at enrollment, not agent-claimed

        from aakaar.api.repositories import agents as agents_repo  # deferred, see above

        with self._session_factory.session() as s:
            agents_repo.mark_connected(
                s,
                agent_id=identity.agent_id,
                os=info.os,
                hostname=info.hostname,
                gui_capable=info.gui_capable,
                agent_version=info.version,
                capabilities=[{"ref": c.ref, "version": c.version} for c in info.capabilities],
                when=datetime.now(UTC),
            )
            s.commit()

        transport = _SessionTransport(self, sid)
        conn = WebSocketAgentConnection(transport, info)
        self._registry.register(conn)
        logger.info(
            "agent connected via broker alias=%s tenant=%s os=%s sid=%s",
            identity.alias,
            identity.tenant_id,
            info.os,
            sid,
        )
        request_handler = getattr(self, "_request_handler", None)
        try:
            await transport.send_json(
                {"type": "welcome", "alias": identity.alias, "pubkey": self._server_pubkey}
            )
            while True:
                frame = await sess.queue.get()
                if frame is _EOF:
                    break
                msg = self._parse_json(frame)
                if msg is None:
                    continue
                await demux_agent_frame(
                    conn,
                    msg,
                    on_event=lambda m: self._relay_event(identity.tenant_id, m),
                    request_handler=request_handler,
                )
        finally:
            conn.fail_pending("agent disconnected")
            # Only drop the registry entry if it is still OURS — a newer
            # connection for the same alias must not be evicted by our teardown.
            if self._registry.get(identity.tenant_id, identity.alias) is conn:
                self._registry.unregister(identity.tenant_id, identity.alias)
            with self._session_factory.session() as s:
                agents_repo.mark_disconnected(
                    s, agent_id=identity.agent_id, when=datetime.now(UTC)
                )
                s.commit()

    @staticmethod
    def _parse_json(frame: str | None) -> dict[str, Any] | None:
        if frame is None:
            return None
        try:
            msg = json.loads(frame)
        except ValueError:
            return None
        return msg if isinstance(msg, dict) else None

    def _relay_event(self, tenant_id: uuid.UUID, msg: dict[str, Any]) -> None:
        if self._recorder is None:
            return
        try:
            run_id = uuid.UUID(str(msg["run_id"]))
            self._recorder.record(
                run_id=run_id,
                tenant_id=tenant_id,
                node_id=msg.get("node_id"),
                kind=str(msg.get("kind", "log")),
                payload=msg.get("payload") or {},
            )
        except Exception:
            logger.debug("broker agent event relay failed", exc_info=True)

    async def _drain_sessions(self) -> None:
        """Master link dropped: unwind every session (the broker has already
        closed the agent sockets on its side; they will re-dial)."""
        tasks = [s.task for s in self._sessions.values() if s.task is not None]
        for sess in list(self._sessions.values()):
            sess.feed(_EOF)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _spawn(self, coro: Any) -> None:
        # The loop only keeps weak refs to tasks; hold one until completion.
        task = asyncio.ensure_future(coro)
        self._aux_tasks.add(task)
        task.add_done_callback(self._aux_tasks.discard)

    # ---------- outbound frames ---------------------------------------------------

    async def send_frame(self, sid: str, frame: str) -> None:
        ws = self._ws
        if ws is None:
            raise ConnectionError("broker master link is down")
        await ws.send(json.dumps({"t": "data", "sid": sid, "frame": frame}))

    async def send_session_close(self, sid: str) -> None:
        ws = self._ws
        if ws is None:
            raise ConnectionError("broker master link is down")
        await ws.send(json.dumps({"t": "close", "sid": sid}))

    async def _send_session_close_quiet(self, sid: str) -> None:
        with contextlib.suppress(Exception):
            await self.send_session_close(sid)


__all__ = [
    "AgentIdentity",
    "BrokerLink",
    "authenticate_agent_key",
    "master_link_url",
    "parse_agent_key",
]
