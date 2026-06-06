"""cap.web_navigate — drive a browser session through a sequence of steps.

A thin, composable wrapper over the `browser.*` primitives for the common
"load a page and walk through a few interactions" intent. It either:

  - opens a *fresh* browser session (when `session_id` is not supplied),
    navigating to `url` first, exactly like `cap.open_url`; or
  - reuses an *existing* session previously stashed in `ctx.session_state`
    (when `session_id` is supplied) — e.g. a session returned by
    `cap.open_url` or `cap.web_login` — and navigates it to `url`.

After the initial navigation it executes each step in order. Supported
step actions:

  - goto:     navigate the session to `value` (an absolute URL).
  - wait_for: wait for the CSS selector `value` to attach to the DOM.
  - click:    click the element matched by the CSS selector `value`.

The session is always stashed (or kept) in `ctx.session_state` under its
id so downstream nodes can keep using the same page. The handler returns
`{session_id, final_url, title}`. `final_url` and `title` are read from
the live page via `evaluate(...)`; if the underlying session can't report
them (older/limited implementations) they fall back to the last navigated
URL and an empty title respectively.

This capability holds no secrets. For login flows use `cap.web_login`
(which fetches credentials from the vault) and feed its session id back
in here via `session_id` to continue navigating authenticated pages.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import CapabilityDefinition

logger = logging.getLogger(__name__)
CAP_REF = "cap.web_navigate"

_DEFAULT_WAIT_TIMEOUT_MS = 30000
_HREF_JS = "window.location.href"
_TITLE_JS = "document.title"


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["goto", "wait_for", "click"] = Field(
        description=(
            "What to do: 'goto' navigates to value (an absolute URL), "
            "'wait_for' waits for the CSS selector value to appear, "
            "'click' clicks the element matched by the CSS selector value."
        )
    )
    value: str = Field(
        min_length=1,
        description=(
            "The target for the action: a URL for 'goto', a CSS selector for "
            "'wait_for' and 'click'."
        ),
    )


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(
        description="Absolute URL to navigate to first (http or https).",
    )
    steps: list[_Step] | None = Field(
        default=None,
        description=(
            "Optional ordered list of steps to perform after the initial "
            "navigation. Each step is {action, value}."
        ),
    )
    session_id: str | None = Field(
        default=None,
        description=(
            "Optional id of an existing browser session (from a prior "
            "cap.open_url / cap.web_login / cap.web_navigate node) to reuse. "
            "When omitted, a fresh session is opened."
        ),
    )
    wait_timeout_ms: int = Field(
        default=_DEFAULT_WAIT_TIMEOUT_MS,
        ge=1000,
        le=120000,
        description="Timeout applied to each 'wait_for' step.",
    )


class _Outputs(BaseModel):
    session_id: str = Field(
        description="Browser session handle for downstream browser.* nodes."
    )
    final_url: str = Field(description="The page URL after all steps ran.")
    title: str = Field(description="The page title after all steps ran (may be empty).")


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Drive a browser session through a sequence of steps (goto / wait_for "
        "/ click). Opens a fresh session and navigates to `url`, or reuses an "
        "existing session via `session_id`. Returns the session id, the final "
        "URL, and the page title. No login; no secrets."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("browser", "navigate"),
)


async def _read_page_state(session: Any, fallback_url: str) -> tuple[str, str]:
    """Best-effort read of the current URL and title via JS evaluation.

    The BrowserSession protocol exposes no direct url/title accessor, so we
    ask the page. Implementations that can't evaluate (or return nothing)
    degrade to the last navigated URL and an empty title — never raise.
    """
    final_url = fallback_url
    title = ""
    try:
        result = await session.evaluate(_HREF_JS)
        if isinstance(result, str) and result:
            final_url = result
    except Exception:  # pragma: no cover - defensive; evaluate is best-effort
        logger.debug("cap.web_navigate could not read location.href", exc_info=True)
    try:
        result = await session.evaluate(_TITLE_JS)
        if isinstance(result, str):
            title = result
    except Exception:  # pragma: no cover - defensive; evaluate is best-effort
        logger.debug("cap.web_navigate could not read document.title", exc_info=True)
    return final_url, title


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    url = inputs["url"]
    raw_steps = inputs.get("steps") or []
    session_id = inputs.get("session_id")
    wait_timeout = int(inputs.get("wait_timeout_ms", _DEFAULT_WAIT_TIMEOUT_MS))

    # Normalize steps: they may arrive as dicts (from JSON DAG inputs) or as
    # already-validated _Step models, depending on the call path.
    steps: list[dict[str, str]] = []
    for s in raw_steps:
        if isinstance(s, _Step):
            steps.append({"action": s.action, "value": s.value})
        else:
            steps.append({"action": s["action"], "value": s["value"]})

    logger.info(
        "cap.web_navigate start run_id=%s url=%s reuse_session=%s steps=%d",
        ctx.run_id,
        url,
        bool(session_id),
        len(steps),
    )

    # Lazily import the browser activity helpers so this module imports even
    # in deployments without the browser worker wired in.
    from aakaar.interpreter.activities.browser import (
        _get_session,
        _SessionHolder,
        _stash_key,
    )

    fresh_cm = None
    if session_id:
        # Reuse: _get_session raises a clear RuntimeError if the id is unknown.
        session = _get_session(ctx, session_id)
        try:
            await session.navigate(url)
        except Exception:
            raise
    else:
        if ctx.browser_pool is None:
            raise RuntimeError(
                "cap.web_navigate requires a browser_pool when no session_id is given"
            )
        fresh_cm = ctx.browser_pool.checkout()
        session = await fresh_cm.__aenter__()
        try:
            await session.navigate(url)
        except Exception:
            await fresh_cm.__aexit__(None, None, None)
            raise

    try:
        for i, step in enumerate(steps):
            action = step["action"]
            value = step["value"]
            logger.debug(
                "cap.web_navigate step %d/%d action=%s value=%r",
                i + 1,
                len(steps),
                action,
                value,
            )
            if action == "goto":
                await session.navigate(value)
            elif action == "wait_for":
                await session.wait_for(value, timeout_ms=wait_timeout)
            elif action == "click":
                await session.click(value)
            else:  # pragma: no cover - guarded by the Literal on _Step
                raise ValueError(f"cap.web_navigate: unknown step action {action!r}")
    except Exception:
        # Only tear down a session we opened in this call. A reused session is
        # owned by whoever stashed it and stays alive for downstream nodes.
        if fresh_cm is not None:
            await fresh_cm.__aexit__(None, None, None)
        raise

    # Stash a freshly opened session so downstream nodes can reuse it. A
    # reused session is already in session_state under its id — leave it.
    if fresh_cm is not None:
        ctx.session_state[_stash_key(session.id)] = _SessionHolder(
            cm=fresh_cm, session=session
        )

    final_url, title = await _read_page_state(session, fallback_url=url)
    logger.info(
        "cap.web_navigate ok run_id=%s session=%s final_url=%s",
        ctx.run_id,
        session.id,
        final_url,
    )
    return {"session_id": session.id, "final_url": final_url, "title": title}
