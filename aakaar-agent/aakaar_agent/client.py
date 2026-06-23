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
import os
import random
import socket
import time
import uuid
from collections import OrderedDict
from typing import Any

import websockets

from aakaar_agent import VERSION
from aakaar_agent.capabilities import advertised, dispatch, load_capabilities
from aakaar_agent.runtime import AgentRuntime, is_uncacheable
from aakaar_agent.session import detect_gui, detect_os
from aakaar_caps.sealing import Sealer

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
        # Back-channel: agent-initiated requests (obj_get/obj_put/signal_open/
        # llm_*) awaiting the server's reply, correlated by request_id.
        self._pending_req: dict[str, asyncio.Future] = {}
        # Server-initiated control handler (run_end/cancel), set by the runtime.
        self._ctrl_handler: Any = None
        # Browser/capability runtime: local Playwright pool + per-run session
        # state + the CapabilityContext builder (proxies route back over the WS).
        self._runtime = AgentRuntime(self)
        self.set_control_handler(self._handle_runtime_ctrl)
        # Sealed-box transport: our keypair (public key sent in hello), and the
        # server's public key (captured from welcome) for sealing obj_put bodies.
        self._sealer = Sealer.generate()
        self._server_pubkey: str | None = None
        # Whether Chromium actually launches here. Until the startup probe runs
        # we assume yes; a failed probe drops browser caps from what we advertise
        # so the server never routes browser work to a half-installed agent.
        self._browser_ok = True

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
        await self._probe_browser()
        try:
            await self._serve_loop()
        finally:
            await self._runtime.shutdown()

    async def _probe_browser(self) -> None:
        """Prove Chromium launches before advertising browser caps. Module-import
        is not proof (load_capabilities swallows import errors), and a launch
        failure must be a loud, surfaced state — not silent. Opt out entirely
        with AAKAAR_AGENT_NO_BROWSER=1 on a desktop-only agent."""
        if os.environ.get("AAKAAR_AGENT_NO_BROWSER", "0") in ("1", "true", "yes"):
            self._browser_ok = False
            logger.info("agent: browser capabilities disabled (AAKAAR_AGENT_NO_BROWSER)")
            return
        self._browser_ok = await self._runtime.launch_probe()
        if self._browser_ok:
            logger.info("agent: Chromium launch probe OK — browser capabilities advertised")
        else:
            logger.warning(
                "agent: Chromium launch probe FAILED — NOT advertising browser caps. "
                "Install with `playwright install chromium` (see start-agent.sh)."
            )

    async def _serve_loop(self) -> None:
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
            "capabilities": self._advertised_caps(),
            "pubkey": self._sealer.public_key_hex(),
        }

    def _advertised_caps(self) -> list[dict]:
        caps = advertised()
        if self._browser_ok:
            return caps
        # Chromium can't launch here: hide browser-family caps so the server's
        # placement never sends us browser work we can't run.
        return [c for c in caps if not is_uncacheable(c.get("ref", ""))]

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
                    kind = msg.get("type")
                    if kind == "task":
                        self._handle_task(msg)
                    elif kind == "reply":
                        self._resolve_reply(msg)
                    elif kind == "ctrl":
                        self._handle_ctrl(msg)
                    elif kind == "welcome":
                        self._server_pubkey = msg.get("pubkey")
                    # unknown: ignore
            finally:
                self._ws = None
                # A dropped socket can never deliver outstanding replies; fail
                # them so any capability blocked on a back-channel request (e.g.
                # an obj_put mid-task) unwinds instead of hanging forever.
                self._fail_pending_requests("agent connection lost")
                # Agent-drop is fatal to a browser session: the server fails the
                # in-flight node and cannot resume the session onto a fresh
                # agent, so reclaim the live Chromium contexts now rather than
                # leak them holding a logged-in banking session.
                await self._runtime.sweep_all()

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
        run_id = msg.get("run_id") or None
        node_id = msg.get("node_id") or None
        tenant_id = msg.get("tenant_id") or None
        inputs = msg.get("inputs") or {}
        secrets = self._unseal_secrets(msg)
        try:
            ctx = self._runtime.build_context(
                secrets=secrets, run_id=run_id, node_id=node_id, tenant_id=tenant_id
            )
            if ref == "browser.open_session" and run_id:
                # Idempotent per node: a retried open_session returns the existing
                # session, never a second Chromium context.
                cached = self._runtime.cached_open(tenant_id, run_id, node_id)
                if cached is not None:
                    outputs = cached
                else:
                    outputs = await dispatch(ref, inputs, secrets, context=ctx)
                    self._runtime.record_open(tenant_id, run_id, node_id, outputs)
            else:
                outputs = await dispatch(ref, inputs, secrets, context=ctx)
            reply = {"type": "result", "task_id": task_id, "ok": True, "outputs": outputs}
        except Exception as e:
            logger.warning("task %s (%s) failed: %s", task_id, ref, e)
            reply = {
                "type": "result",
                "task_id": task_id,
                "ok": False,
                "error": {"type": type(e).__name__, "message": str(e)[:500]},
            }
        # Live preview: after a successful browser node, screenshot the live
        # session and emit a live_screen event (the session is here, not on the
        # server). The server flags only browser refs, and only when its
        # live_screenshots setting is on.
        if reply.get("ok") and msg.get("live_screen") and run_id:
            uri = await self._runtime.live_screen(tenant_id, run_id, node_id)
            if uri:
                await self._emit_event(run_id, node_id, "live_screen", {"uri": uri})
        # Cache the reply for reconnect re-delivery — but NEVER for browser /
        # session / screenshot / download / web refs: those carry a live session
        # id (meaningless after teardown) or sensitive bytes, and must not sit in
        # the LRU. Stateless utility/desktop caps stay cacheable.
        if task_id and not is_uncacheable(ref):
            self._results[task_id] = reply
            self._results.move_to_end(task_id)
            while len(self._results) > _RESULT_CACHE_MAX:
                evicted, _ = self._results.popitem(last=False)
                self._undelivered.discard(evicted)
        await self._send_reply(reply)

    async def _emit_event(self, run_id: str, node_id: str | None, kind: str, payload: dict) -> None:
        """Send an agent->server run-timeline event (best-effort)."""
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(json.dumps({
                "type": "event", "run_id": run_id, "node_id": node_id, "kind": kind, "payload": payload,
            }))
        except Exception:  # noqa: BLE001
            logger.debug("event %s for run %s not sent", kind, run_id)

    def _unseal_secrets(self, msg: dict) -> dict:
        """Unseal the credential envelope sealed to our public key; fall back to
        a cleartext `secrets` field for an unsealed (legacy/no-crypto) server."""
        env = msg.get("secrets_sealed")
        if env:
            return json.loads(self._sealer.unseal(env).decode("utf-8"))
        return msg.get("secrets") or {}

    async def _handle_runtime_ctrl(self, msg: dict) -> None:
        """Server-initiated control: end a run's sessions, or cancel a task."""
        op = msg.get("op")
        tenant_id = msg.get("tenant_id") or None
        run_id = msg.get("run_id") or None
        if op == "run_end":
            await self._runtime.end_run(tenant_id, run_id)
        elif op == "cancel":
            task_id = msg.get("task_id")
            if task_id and task_id in self._inflight:
                self._inflight[task_id].cancel()
            if run_id:
                await self._runtime.end_run(tenant_id, run_id)

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

    # ---- back-channel: agent -> server requests + server -> agent control ----

    def set_control_handler(self, handler: Any) -> None:
        """Register an async ``handler(msg)`` invoked for server ``ctrl`` frames
        (run_end / cancel). The runtime sets this; until then ctrl frames are
        acked as no-ops."""
        self._ctrl_handler = handler

    async def send_request(self, op: str, **payload: Any) -> dict:
        """Agent-initiated request to the server (obj_get / obj_put /
        signal_open / llm_complete / llm_plan). Returns the server's ``result``
        dict, or raises on a server error or a dropped connection. No local
        timeout — the server bounds the op (and the task deadline bounds us)."""
        ws = self._ws
        loop = self._loop
        if ws is None or loop is None:
            raise ConnectionError("not connected")
        request_id = uuid.uuid4().hex
        fut: asyncio.Future = loop.create_future()
        self._pending_req[request_id] = fut
        try:
            await ws.send(json.dumps({"type": "req", "request_id": request_id, "op": op, **payload}))
            return await fut
        finally:
            self._pending_req.pop(request_id, None)

    def _resolve_reply(self, msg: dict) -> None:
        fut = self._pending_req.get(str(msg.get("request_id", "")))
        if fut is None or fut.done():
            return
        if msg.get("ok"):
            fut.set_result(msg.get("result") or {})
        else:
            err = msg.get("error") or {}
            fut.set_exception(RuntimeError(err.get("message", "remote request failed")))

    def _fail_pending_requests(self, reason: str) -> None:
        for fut in self._pending_req.values():
            if not fut.done():
                fut.set_exception(ConnectionError(reason))
        self._pending_req.clear()

    def _handle_ctrl(self, msg: dict) -> None:
        self._spawn(self._run_ctrl(msg))

    async def _run_ctrl(self, msg: dict) -> None:
        request_id = msg.get("request_id")
        ok, error = True, None
        try:
            handler = self._ctrl_handler
            if handler is not None:
                await handler(msg)
        except Exception as e:  # noqa: BLE001
            ok = False
            error = {"type": type(e).__name__, "message": str(e)[:500]}
            logger.warning("ctrl op %s failed: %s", msg.get("op"), e)
        if request_id:
            await self._send_ack(request_id, ok, error)

    async def _send_ack(self, request_id: str, ok: bool, error: dict | None) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(json.dumps({"type": "ack", "request_id": request_id, "ok": ok, "error": error}))
        except Exception:  # noqa: BLE001 - best-effort; server falls back to timeout
            logger.debug("ack for request %s not sent", request_id)
