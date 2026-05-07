"""browser.* activity handlers.

A run's first browser activity (`browser.open_session`) checks out a
session from the pool and stashes it in `session_state` under its id.
Downstream browser activities look up the session by the id passed in
their `session` input.

The session is closed when:
  - the workflow explicitly calls `browser.close_session`, OR
  - the orchestrator's run-end cleanup runs (catches leaked sessions)

Capabilities (PR-5+) get a higher-level helper that wraps these primitives;
see `aakar/capabilities/_base.py`.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import Any

from aakar.interpreter.activities.registry import ActivityRegistry
from aakar.interpreter.activities.types import ActivityContext
from aakar.workers.browser.session import BrowserSession


_SESSION_PREFIX = "browser:"


def _stash_key(session_id: str) -> str:
    return f"{_SESSION_PREFIX}{session_id}"


def _get_session(ctx: ActivityContext, session_id: str) -> BrowserSession:
    holder = ctx.session_state.get(_stash_key(session_id))
    if holder is None:
        raise RuntimeError(
            f"no live browser session for id {session_id!r}; was browser.open_session called?"
        )
    return holder.session


# ---------- handlers -------------------------------------------------------


async def open_session(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    if ctx.browser_pool is None:
        raise RuntimeError(
            "browser activities require a browser_pool on ActivityContext; none configured"
        )
    profile = inputs.get("profile")
    # Pool checkout is a context manager; we manually enter and stash a
    # closer in session_state so the orchestrator's cleanup releases it.
    cm = ctx.browser_pool.checkout(profile=profile)
    session = await cm.__aenter__()

    holder = _SessionHolder(cm=cm, session=session)
    ctx.session_state[_stash_key(session.id)] = holder
    return {"session": session.id}


async def navigate(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = _get_session(ctx, inputs["session"])
    await sess.navigate(inputs["url"])
    return {}


async def wait_for(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = _get_session(ctx, inputs["session"])
    await sess.wait_for(inputs["selector"], timeout_ms=int(inputs.get("timeout_ms", 30000)))
    return {}


async def fill(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = _get_session(ctx, inputs["session"])
    await sess.fill(inputs["selector"], inputs["value"])
    return {}


async def fill_secret(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Fill a field with a vault-stored secret. Lets the planner compose
    multi-step login flows (e.g. with a captcha mid-form) without ever
    embedding the secret value as a literal in the DAG.

    Inputs:
      - session, selector: as in browser.fill
      - capability_ref, account_alias: which grant to read from
      - secret_name: which key inside the grant's secret bundle to use

    The value is fetched fresh per call. It does not appear in the DAG, in
    run-event payloads, or in node outputs.
    """
    from aakar.interpreter.credentials import fetch_credentials

    creds = fetch_credentials(
        ctx,
        capability_ref=inputs["capability_ref"],
        account_alias=inputs["account_alias"],
    )
    secret_name = inputs["secret_name"]
    if secret_name not in creds:
        raise PermissionError(
            f"grant {inputs['capability_ref']!r}/{inputs['account_alias']!r} has no "
            f"secret named {secret_name!r}"
        )
    sess = _get_session(ctx, inputs["session"])
    await sess.fill(inputs["selector"], creds[secret_name])
    return {}


async def click(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = _get_session(ctx, inputs["session"])
    await sess.click(inputs["selector"])
    return {}


async def select(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = _get_session(ctx, inputs["session"])
    await sess.select(inputs["selector"], inputs["value"])
    return {}


async def upload(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = _get_session(ctx, inputs["session"])
    file_uri = inputs["file_uri"]
    # Materialize the managed-storage file to a temp path Playwright can read.
    if not file_uri.startswith("aakar://"):
        raise ValueError(f"file_uri must be a managed-storage URI, got {file_uri!r}")
    data = ctx.object_store.get(file_uri)
    fd = tempfile.NamedTemporaryFile(delete=False)
    try:
        fd.write(data)
    finally:
        fd.close()
    try:
        await sess.upload(inputs["selector"], fd.name)
    finally:
        Path(fd.name).unlink(missing_ok=True)
    return {}


async def download(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = _get_session(ctx, inputs["session"])
    trigger = inputs.get("trigger_selector")
    url = inputs.get("url")
    file = await sess.download(trigger_selector=trigger, url=url)
    key = f"runs/{ctx.run_id}/downloads/{uuid.uuid4().hex}_{file.filename}"
    obj = ctx.object_store.put(str(ctx.tenant_id), key, file.content)
    return {"file_uri": obj.uri}


async def extract(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = _get_session(ctx, inputs["session"])
    extracted = await sess.extract(
        inputs["selector"], attribute=inputs.get("attribute", "text")
    )
    return {"value": extracted.value}


async def screenshot(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = _get_session(ctx, inputs["session"])
    image = await sess.screenshot()
    key = f"runs/{ctx.run_id}/screenshots/{uuid.uuid4().hex}.png"
    obj = ctx.object_store.put(str(ctx.tenant_id), key, image)
    return {"image_uri": obj.uri}


async def close_session(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    session_id = inputs["session"]
    holder = ctx.session_state.pop(_stash_key(session_id), None)
    if holder is not None:
        await holder.close()
    return {}


def register_into(reg: ActivityRegistry) -> None:
    reg.register("browser.open_session", open_session)
    reg.register("browser.navigate", navigate)
    reg.register("browser.wait_for", wait_for)
    reg.register("browser.fill", fill)
    reg.register("browser.fill_secret", fill_secret)
    reg.register("browser.click", click)
    reg.register("browser.select", select)
    reg.register("browser.upload", upload)
    reg.register("browser.download", download)
    reg.register("browser.extract", extract)
    reg.register("browser.screenshot", screenshot)
    reg.register("browser.close_session", close_session)


# ---------- internals ------------------------------------------------------


class _SessionHolder:
    """Lives in `ActivityContext.session_state`. Keeps the session reachable
    by activities (`.session`) and the underlying checkout context manager
    closeable by the orchestrator's run-end cleanup (`.close()`)."""

    def __init__(self, cm, session: BrowserSession) -> None:
        self._cm = cm
        self.session = session
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._cm.__aexit__(None, None, None)
        except Exception:
            pass
