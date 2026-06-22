"""Bidirectional back-channel demux — the single router for frames arriving
from an agent, shared by BOTH server read loops (the direct ``/ws/agents``
endpoint and the broker-relayed link) so they can never drift apart.

On top of the existing task/result flow, two request/response fabrics ride the
one agent socket:

  agent  -> server  req   {type:"req",  request_id, op, ...}
  server -> agent   reply {type:"reply", request_id, ok, result|error}

  server -> agent   ctrl  {type:"ctrl", request_id, op, ...}
  agent  -> server  ack   {type:"ack",  request_id, ok, error?}

So the frames a server read loop receives FROM an agent are: ``result`` (task
result), ``event`` (run-timeline progress), ``ack`` (reply to a server ctrl),
and ``req`` (an agent-initiated request — object I/O, HITL signals, LLM calls).

``req`` frames are handed to ``request_handler``. Until the back-channel is
enabled (Stage 4 wires a real handler with server-side authz), ``request_handler``
is None and we reply with a clear "not enabled" error so a misconfigured agent
fails fast instead of hanging.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

# Hard ceilings on agent-initiated requests so a compromised/buggy agent can't
# turn the back-channel into an unbounded object-write or free LLM oracle.
_MAX_OBJ_BYTES = 32 * 1024 * 1024  # matches the broker frame cap headroom
_MAX_LLM_CALLS_PER_RUN = 200

# (conn, msg) -> None. The handler processes an agent `req` and is responsible
# for sending exactly one `reply` back via conn.send_reply(...).
RequestHandler = Callable[[Any, dict[str, Any]], Awaitable[None]]

# (msg) -> None. Records an agent-emitted run-timeline event.
EventSink = Callable[[dict[str, Any]], None]


async def demux_agent_frame(
    conn: Any,
    msg: dict[str, Any],
    *,
    on_event: EventSink,
    request_handler: RequestHandler | None = None,
) -> None:
    kind = msg.get("type")
    if kind == "result":
        conn.resolve_result(msg)
    elif kind == "event":
        on_event(msg)
    elif kind == "ack":
        conn.resolve_ack(msg)
    elif kind == "req":
        if request_handler is not None:
            await request_handler(conn, msg)
        else:
            await conn.send_reply(
                msg.get("request_id"),
                ok=False,
                error={
                    "type": "Disabled",
                    "message": "agent back-channel is not enabled on this server",
                },
            )
    # welcome/ping/pong/unknown: ignore (WS keepalive handles liveness)


class ServerBackchannelHandler:
    """Processes agent-initiated `req` frames (obj_get / obj_put / llm_complete /
    llm_plan / signal_open) against the server's real services and replies.

    Security invariants:
      - The tenant is taken from the AUTHENTICATED connection (``conn.info``),
        NEVER from the request body. obj_get additionally enforces the
        object-store tenant prefix, so an agent can't read another tenant's blob.
      - obj_put bodies are size-capped; llm_* calls are rate-capped per run so a
        rogue agent can't exhaust storage or the OpenAI budget.

    Object-store and LLM clients are synchronous; we run them in a thread so the
    one agent socket's read loop is never blocked.
    """

    def __init__(self, *, object_store: Any, llm: Any = None, signals: Any = None, sealer: Any = None) -> None:
        self._object_store = object_store
        self._llm = llm
        self._signals = signals
        self._sealer = sealer
        self._llm_calls: dict[str, int] = {}

    async def __call__(self, conn: Any, msg: dict[str, Any]) -> None:
        request_id = msg.get("request_id")
        op = msg.get("op")
        tenant_id = str(getattr(conn.info, "tenant_id", "") or "")
        try:
            result = await self._handle(op, msg, tenant_id, conn)
            await conn.send_reply(request_id, ok=True, result=result)
        except Exception as e:  # noqa: BLE001 - reply with the error, never crash the loop
            logger.warning("back-channel op %s failed: %s", op, e)
            await conn.send_reply(
                request_id, ok=False, error={"type": type(e).__name__, "message": str(e)[:500]}
            )

    async def _handle(self, op: str | None, msg: dict[str, Any], tenant_id: str, conn: Any) -> dict[str, Any]:
        if op == "obj_put":
            return await self._obj_put(msg, tenant_id)
        if op == "obj_get":
            return await self._obj_get(msg, tenant_id, conn)
        if op == "llm_complete":
            return await self._llm_complete(msg)
        if op == "llm_plan":
            return await self._llm_plan(msg)
        if op == "signal_open":
            return await self._signal_open(msg, tenant_id)
        raise ValueError(f"unknown back-channel op {op!r}")

    def _read_body(self, msg: dict[str, Any]) -> bytes:
        """Decode an inbound object body — sealed (preferred) or base64."""
        sealed = msg.get("sealed")
        if sealed is not None:
            if self._sealer is None:
                raise RuntimeError("received a sealed body but no sealer is configured")
            return self._sealer.unseal(sealed)
        return base64.b64decode(msg.get("b64") or "")

    def _emit_body(self, data: bytes, conn: Any) -> dict[str, Any]:
        """Encode an outbound object body, sealed to the agent's key when both
        the sealer and the agent's public key are available; else base64."""
        pub = getattr(conn.info, "public_key", None)
        if self._sealer is not None and pub:
            env = self._sealer.seal(data, pub)
            if env is not None:
                return {"sealed": env}
        return {"b64": base64.b64encode(data).decode("ascii")}

    async def _obj_put(self, msg: dict[str, Any], tenant_id: str) -> dict[str, Any]:
        data = self._read_body(msg)
        if len(data) > _MAX_OBJ_BYTES:
            raise ValueError(f"obj_put body too large ({len(data)} bytes)")
        key = str(msg["key"])
        obj = await asyncio.to_thread(self._object_store.put, tenant_id, key, data)
        return {"uri": obj.uri}

    async def _obj_get(self, msg: dict[str, Any], tenant_id: str, conn: Any) -> dict[str, Any]:
        from aakaar.storage.object_store import parse_uri

        uri = str(msg["uri"])
        owner, _key = parse_uri(uri)
        if owner != tenant_id:
            # The agent may only read blobs belonging to its own tenant.
            raise PermissionError("cross-tenant object read denied")
        data = await asyncio.to_thread(self._object_store.get, uri)
        return self._emit_body(data, conn)

    def _check_llm_budget(self, msg: dict[str, Any]) -> None:
        if self._llm is None:
            raise RuntimeError("no LLM configured on this server")
        run_id = str(msg.get("run_id") or "_norun")
        n = self._llm_calls.get(run_id, 0) + 1
        if n > _MAX_LLM_CALLS_PER_RUN:
            raise PermissionError(f"LLM call budget exhausted for run {run_id}")
        self._llm_calls[run_id] = n

    async def _llm_complete(self, msg: dict[str, Any]) -> dict[str, Any]:
        self._check_llm_budget(msg)
        text = await asyncio.to_thread(
            self._llm.complete_text, str(msg.get("system", "")), str(msg.get("user", ""))
        )
        return {"text": text or ""}

    async def _llm_plan(self, msg: dict[str, Any]) -> dict[str, Any]:
        from aakaar.planner.llm import LLMMessage, Role

        self._check_llm_budget(msg)
        messages = [
            LLMMessage(role=Role(str(m.get("role", "user"))), content=str(m.get("content", "")))
            for m in (msg.get("messages") or [])
        ]
        completion = await asyncio.to_thread(self._llm.complete_planner, messages)
        return {"text": getattr(completion, "rationale", "") or ""}

    async def _signal_open(self, msg: dict[str, Any], tenant_id: str) -> dict[str, Any]:
        import uuid as _uuid

        if self._signals is None:
            raise RuntimeError("no signal hub configured on this server")
        prompt = await self._signals.open(
            run_id=_uuid.UUID(str(msg["run_id"])),
            node_id=str(msg.get("node_id") or ""),
            message=str(msg.get("message", "")),
            expects=str(msg.get("expects", "text")),
        )
        response = await prompt.future
        return {"response": response}


__all__ = ["demux_agent_frame", "RequestHandler", "EventSink", "ServerBackchannelHandler"]
