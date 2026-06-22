"""The 14 ``browser.*`` primitives as shared capabilities.

These were the server-only activity handlers in
``aakaar.interpreter.activities.browser``; the logic now lives here so the SAME
code runs on the server and on a remote agent that drives a local browser. Each
``run`` programs only against the portable ``CapabilityContext`` surface:
``browser_pool`` + ``session_state`` (local on whichever host), and
``write_object``/``read_object`` (the canonical object store, a WS-RPC proxy on
the agent). ``fill_secret`` reads its credential from ``ctx.secrets`` — the
server resolves it from the vault at dispatch; the agent receives it sealed.

Catalog note: the ``browser.*`` refs are already registered as
``ActionDefinition``s by ``build_default_registry()`` (server source of truth
for planning/placement/validation), so ``register_shared`` skips them and never
builds a ``CapabilityDefinition`` from the placeholder schemas below. The schemas
exist only to satisfy ``CapabilitySpec``; the agent advertises ref+version and
executes ``run`` without schema validation. The server runs the same ``run`` via
an ``ActivityContext``→``CapabilityContext`` adapter in
``aakaar.interpreter.activities.browser.register_into``.
"""

from __future__ import annotations

import logging
import tempfile
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from aakaar_caps.browser.state import SessionHolder, get_session, stash_key
from aakaar_caps.context import CapabilityContext, CapabilityError
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)


# Placeholder schemas — see module docstring (not used for the server catalog).
class _In(BaseModel):
    model_config = ConfigDict(extra="allow")


class _Out(BaseModel):
    model_config = ConfigDict(extra="allow")


# ---------- handlers (run(ctx, inputs) -> dict) ----------------------------


async def _open_session(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    if ctx.browser_pool is None:
        raise CapabilityError(
            "browser activities require a browser_pool on the context; none configured"
        )
    profile = inputs.get("profile")
    logger.debug("browser.open_session run_id=%s profile=%s", ctx.run_id, profile)
    cm = ctx.browser_pool.checkout(profile=profile)
    session = await cm.__aenter__()
    ctx.session_state[stash_key(session.id)] = SessionHolder(cm=cm, session=session)
    logger.info("browser.open_session run_id=%s session=%s", ctx.run_id, session.id)
    return {"session": session.id}


async def _navigate(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = get_session(ctx.session_state, inputs["session"])
    logger.info("browser.navigate session=%s url=%s", inputs["session"], inputs["url"])
    await sess.navigate(inputs["url"])
    return {}


async def _wait_for(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = get_session(ctx.session_state, inputs["session"])
    timeout_ms = int(inputs.get("timeout_ms", 30000))
    await sess.wait_for(inputs["selector"], timeout_ms=timeout_ms)
    return {}


async def _fill(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = get_session(ctx.session_state, inputs["session"])
    await sess.fill(inputs["selector"], inputs["value"])
    return {}


async def _fill_secret(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Fill a field with a vault-stored secret. The secret arrives in
    ``ctx.secrets`` (server: resolved from the vault by the dispatch adapter;
    agent: shipped sealed). It never appears in the DAG, run events, or node
    outputs."""
    secret_name = inputs["secret_name"]
    if secret_name not in ctx.secrets:
        logger.warning(
            "browser.fill_secret missing secret cap=%s alias=%s name=%s",
            inputs.get("capability_ref"),
            inputs.get("account_alias"),
            secret_name,
        )
        raise PermissionError(
            f"grant {inputs.get('capability_ref')!r}/{inputs.get('account_alias')!r} has no "
            f"secret named {secret_name!r}"
        )
    sess = get_session(ctx.session_state, inputs["session"])
    logger.debug(
        "browser.fill_secret session=%s selector=%r name=%s",
        inputs["session"],
        inputs["selector"],
        secret_name,
    )
    await sess.fill(inputs["selector"], ctx.secrets[secret_name])
    return {}


async def _click(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = get_session(ctx.session_state, inputs["session"])
    await sess.click(inputs["selector"])
    return {}


async def _click_by_text(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = get_session(ctx.session_state, inputs["session"])
    logger.debug("browser.click_by_text session=%s text=%r", inputs["session"], inputs["text"])
    await sess.click_by_text(inputs["text"])
    return {}


async def _select(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = get_session(ctx.session_state, inputs["session"])
    await sess.select(inputs["selector"], inputs["value"])
    return {}


async def _set_field(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = get_session(ctx.session_state, inputs["session"])
    await sess.set_field(inputs["label"], inputs["value"])
    return {}


async def _upload(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = get_session(ctx.session_state, inputs["session"])
    file_uri = inputs["file_uri"]
    if not file_uri.startswith("aakaar://"):
        raise ValueError(f"file_uri must be a managed-storage URI, got {file_uri!r}")
    logger.info("browser.upload session=%s selector=%r file_uri=%s", inputs["session"], inputs["selector"], file_uri)
    data = await ctx.read_object(file_uri)
    # delete=False so the path stays valid after the with-block; we unlink below.
    with tempfile.NamedTemporaryFile(delete=False) as fd:
        fd.write(data)
        tmp_path = fd.name
    try:
        await sess.upload(inputs["selector"], tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return {}


async def _download(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = get_session(ctx.session_state, inputs["session"])
    trigger = inputs.get("trigger_selector")
    url = inputs.get("url")
    logger.info("browser.download session=%s trigger=%r url=%s", inputs["session"], trigger, url)
    file = await sess.download(trigger_selector=trigger, url=url)
    key = f"runs/{ctx.run_id}/downloads/{uuid.uuid4().hex}_{file.filename}"
    uri = await ctx.write_object(key, file.content)
    logger.info("browser.download stored uri=%s filename=%s bytes=%d", uri, file.filename, len(file.content))
    return {"file_uri": uri}


async def _extract(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = get_session(ctx.session_state, inputs["session"])
    extracted = await sess.extract(inputs["selector"], attribute=inputs.get("attribute", "text"))
    return {"value": extracted.value}


async def _screenshot(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = get_session(ctx.session_state, inputs["session"])
    image = await sess.screenshot()
    key = f"runs/{ctx.run_id}/screenshots/{uuid.uuid4().hex}.png"
    uri = await ctx.write_object(key, image)
    return {"image_uri": uri}


async def _close_session(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    session_id = inputs["session"]
    logger.info("browser.close_session session=%s", session_id)
    holder = ctx.session_state.pop(stash_key(session_id), None)
    if holder is not None:
        await holder.close()
    return {}


# ---------- specs ----------------------------------------------------------


def _spec(ref: str, *, stateful_session: bool = False) -> CapabilitySpec:
    return CapabilitySpec(
        ref=ref,
        description=f"{ref} (browser primitive)",
        input_schema=_In,
        output_schema=_Out,
        tags=("browser",),
        stateful_session=stateful_session,
    )


# (spec, run) for each primitive. ``SPECS`` (plural) is honored by the loader
# alongside the single-``SPEC`` convention so all 14 live in one module.
SPECS: list[tuple[CapabilitySpec, Any]] = [
    (_spec("browser.open_session", stateful_session=True), _open_session),
    (_spec("browser.navigate"), _navigate),
    (_spec("browser.wait_for"), _wait_for),
    (_spec("browser.fill"), _fill),
    (_spec("browser.fill_secret"), _fill_secret),
    (_spec("browser.click"), _click),
    (_spec("browser.click_by_text"), _click_by_text),
    (_spec("browser.select"), _select),
    (_spec("browser.set_field"), _set_field),
    (_spec("browser.upload"), _upload),
    (_spec("browser.download"), _download),
    (_spec("browser.extract"), _extract),
    (_spec("browser.screenshot"), _screenshot),
    (_spec("browser.close_session"), _close_session),
]
