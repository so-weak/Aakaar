"""Agent-side browser/capability runtime.

The agent runs the SAME shared capability code as the server (``aakaar_caps``),
so it must present the same ``CapabilityContext`` surface. The server fills that
surface from its in-process services; the agent fills it from:

  - a LOCAL Playwright pool (the live Chromium lives here — headed by default,
    since a human may need to see/solve captchas on the agent's screen), and
  - WS-RPC PROXIES back to the server for everything server-owned: the object
    store (``obj_get``/``obj_put``), the planner/LLM (``llm_complete``/``llm_plan``),
    and (later) the HITL signal hub. The agent never holds the object store, the
    vault, or the OpenAI key.

State that genuinely lives on the agent is the browser session: ``open_session``
in one node must be visible to ``navigate``/``click`` in later nodes of the SAME
run. So session_state is kept per ``(tenant_id, run_id)`` and torn down when the
server signals ``run_end`` (or the socket drops). ``open_session`` is idempotent
per node so a retried dispatch never opens a second Chromium.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import uuid
from typing import Any

logger = logging.getLogger(__name__)


def _headless_default() -> bool:
    # Headed by default on the agent (a human may watch / solve a captcha).
    # AAKAAR_AGENT_HEADLESS=1 forces headless (e.g. a server-room agent).
    return os.environ.get("AAKAAR_AGENT_HEADLESS", "0") in ("1", "true", "TRUE", "yes")


def _ignore_https_errors_default() -> bool:
    # Accept untrusted TLS certs (self-signed / internal-CA UAT portals, or a
    # TLS-intercepting proxy). Off unless explicitly enabled for a trusted env.
    val = os.environ.get(
        "AAKAAR_AGENT_BROWSER_IGNORE_HTTPS_ERRORS",
        os.environ.get("AAKAAR_BROWSER_IGNORE_HTTPS_ERRORS", "0"),
    )
    return val.lower() in ("1", "true", "yes")


# Refs whose results must NEVER be cached/re-delivered by the client: they either
# carry sensitive bytes (screenshots/statements), hold a live session id that is
# meaningless after teardown, or are side-effecting. Stateless utility caps and
# desktop caps remain cacheable for the historical reconnect re-delivery.
def is_uncacheable(ref: str) -> bool:
    if ref.startswith("browser."):
        return True
    return ref in {"cap.open_url", "cap.screenshot", "cap.file_download"} or ref.startswith("cap.web")


class AgentRuntime:
    """Owns the agent's browser pool + per-run session state and builds the
    CapabilityContext for each dispatched task.

    ``pool_factory`` is injectable so tests use ``FakeBrowserPool`` without real
    Chromium. The default lazily constructs a Playwright pool the first time a
    browser cap actually runs (so a non-browser agent never starts Chromium)."""

    def __init__(self, client: Any, *, pool_factory: Any = None, headless: bool | None = None) -> None:
        self._client = client
        self._headless = _headless_default() if headless is None else headless
        self._ignore_https_errors = _ignore_https_errors_default()
        self._pool_factory = pool_factory
        self._pool: Any = None
        # (tenant_id, run_id) -> session_state dict shared across that run's nodes.
        self._run_sessions: dict[tuple[str, str], dict[str, Any]] = {}
        # (tenant_id, run_id, node_id) -> open_session outputs (idempotency).
        self._opened: dict[tuple[str, str, str], dict[str, Any]] = {}

    # ---- pool ----------------------------------------------------------------

    @property
    def pool(self) -> Any:
        if self._pool is None:
            if self._pool_factory is not None:
                self._pool = self._pool_factory()
            else:
                from aakaar_caps.browser.playwright import PlaywrightBrowserPool

                self._pool = PlaywrightBrowserPool(
                    headless=self._headless, ignore_https_errors=self._ignore_https_errors
                )
                logger.info(
                    "agent browser pool created (headless=%s, ignore_https_errors=%s)",
                    self._headless,
                    self._ignore_https_errors,
                )
        return self._pool

    async def launch_probe(self) -> bool:
        """Prove Chromium actually launches (module-import is not proof). Used by
        the startup probe so a half-installed agent never advertises browser
        capability. Returns True on success."""
        try:
            async with self.pool.checkout() as _sess:
                pass
            return True
        except Exception:  # noqa: BLE001
            logger.warning("agent browser launch probe failed", exc_info=True)
            return False

    # ---- per-run session state ----------------------------------------------

    @staticmethod
    def _key(tenant_id: str | None, run_id: str | None) -> tuple[str, str]:
        return (tenant_id or "", run_id or "")

    def _run_state(self, tenant_id: str | None, run_id: str | None) -> dict[str, Any]:
        return self._run_sessions.setdefault(self._key(tenant_id, run_id), {})

    def cached_open(self, tenant_id: str | None, run_id: str | None, node_id: str | None) -> dict[str, Any] | None:
        if not run_id or not node_id:
            return None
        return self._opened.get((tenant_id or "", run_id, node_id))

    def record_open(self, tenant_id: str | None, run_id: str | None, node_id: str | None, outputs: dict[str, Any]) -> None:
        if run_id and node_id:
            self._opened[(tenant_id or "", run_id, node_id)] = outputs

    async def end_run(self, tenant_id: str | None, run_id: str | None) -> None:
        """Close every live session for a run and forget its state. Called on the
        server's run_end ctrl and on socket-drop sweeps."""
        key = self._key(tenant_id, run_id)
        state = self._run_sessions.pop(key, None)
        for k in [k for k in self._opened if (k[0], k[1]) == key]:
            self._opened.pop(k, None)
        if not state:
            return
        for holder in list(state.values()):
            close = getattr(holder, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    logger.debug("end_run: session close raised", exc_info=True)
        logger.info("agent run_end: closed %d session(s) run=%s", len(state), run_id)

    async def sweep_all(self) -> None:
        """Close all sessions across all runs — used on socket drop so a
        reconnected agent never claims a session it can't prove is live."""
        for tenant_id, run_id in list(self._run_sessions.keys()):
            await self.end_run(tenant_id, run_id)

    async def shutdown(self) -> None:
        await self.sweep_all()
        if self._pool is not None:
            shutdown = getattr(self._pool, "shutdown", None)
            if shutdown is not None:
                try:
                    await shutdown()
                except Exception:  # noqa: BLE001
                    logger.debug("pool shutdown raised", exc_info=True)
            self._pool = None

    # ---- context -------------------------------------------------------------

    def build_context(
        self,
        *,
        secrets: dict[str, str],
        run_id: str | None,
        node_id: str | None,
        tenant_id: str | None,
    ) -> Any:
        from aakaar_caps.context import CapabilityContext

        client = self._client
        session_state = self._run_state(tenant_id, run_id) if run_id else {}
        sealer = getattr(client, "_sealer", None)
        server_pub = getattr(client, "_server_pubkey", None)

        async def _reader(uri: str) -> bytes:
            res = await client.send_request("obj_get", uri=uri)
            sealed = res.get("sealed")
            if sealed is not None and sealer is not None:
                return sealer.unseal(sealed)
            return base64.b64decode(res.get("b64") or "")

        async def _writer(key: str, data: bytes) -> str:
            return await self._put_object(key, data)

        def _text(system: str, user: str) -> str:
            res = self._call_sync(client.send_request("llm_complete", system=system, user=user))
            return str((res or {}).get("text", ""))

        def _plan(messages: Any) -> str:
            wire = [{"role": str(getattr(m, "role", "")), "content": getattr(m, "content", "")} for m in messages]
            res = self._call_sync(client.send_request("llm_plan", messages=wire))
            return str((res or {}).get("text", ""))

        async def _signal(message: str, expects: str) -> str:
            # Proxy the HITL prompt to the server's signal hub; the human answers
            # in the chat UI regardless of where the browser runs.
            res = await client.send_request(
                "signal_open", run_id=run_id or "", node_id=node_id or "", message=message, expects=expects
            )
            return str((res or {}).get("response", ""))

        return CapabilityContext(
            secrets=dict(secrets or {}),
            tenant_id=tenant_id,
            run_id=run_id,
            node_id=node_id or None,
            object_reader=_reader,
            object_writer=_writer,
            text_completer=_text,
            planner_completer=_plan,
            browser_pool=self.pool,
            session_state=session_state,
            signal_opener=_signal,
        )

    async def _put_object(self, key: str, data: bytes) -> str:
        """Store bytes in the SERVER object store via the back-channel, sealing
        the body to the server's key (broker relays ciphertext). Returns the
        canonical aakaar:// URI."""
        client = self._client
        sealer = getattr(client, "_sealer", None)
        server_pub = getattr(client, "_server_pubkey", None)
        env = sealer.seal(data, server_pub) if sealer is not None else None
        payload = {"sealed": env} if env is not None else {"b64": base64.b64encode(data).decode("ascii")}
        res = await client.send_request("obj_put", key=key, **payload)
        return str(res["uri"])

    async def live_screen(self, tenant_id: str | None, run_id: str | None, node_id: str | None) -> str | None:
        """Screenshot the run's most-recently-active session, upload it, and
        return the URI — the agent equivalent of the executor's per-node live
        screenshot (the live session is here, not on the server). None if there's
        no session or the capture fails."""
        if not run_id:
            return None
        state = self._run_sessions.get(self._key(tenant_id, run_id))
        if not state:
            return None
        holder = list(state.values())[-1]
        sess = getattr(holder, "session", None)
        if sess is None or not hasattr(sess, "screenshot"):
            return None
        try:
            png = await sess.screenshot()
            key = f"runs/{run_id}/livescreen/{uuid.uuid4().hex}.png"
            return await self._put_object(key, png)
        except Exception:  # noqa: BLE001 - a preview must never fail the node
            logger.debug("live_screen capture failed run=%s node=%s", run_id, node_id, exc_info=True)
            return None

    def _call_sync(self, coro: Any) -> Any:
        """Bridge a sync capability seam (complete_text / complete_plan, which
        callers invoke via asyncio.to_thread) to the agent's async back-channel.
        Runs the coroutine on the client loop and blocks the worker thread."""
        loop = getattr(self._client, "_loop", None)
        if loop is None:
            raise RuntimeError("agent loop not running")
        return asyncio.run_coroutine_threadsafe(coro, loop).result()
