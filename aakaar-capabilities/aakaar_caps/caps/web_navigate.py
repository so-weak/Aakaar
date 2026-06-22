"""cap.web_navigate — drive a session through goto/wait_for/click steps.

Opens a fresh session at `url` (or reuses `session_id`), runs an optional step
list, and returns the final URL + title. Shared: identical on server and agent.
No login, no secrets.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aakaar_caps.browser.state import SessionHolder, get_session, stash_key
from aakaar_caps.context import CapabilityContext, CapabilityError
from aakaar_caps.spec import CapabilitySpec

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
        description="The target for the action: a URL for 'goto', a CSS selector for 'wait_for' and 'click'.",
    )


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(description="Absolute URL to navigate to first (http or https).")
    steps: list[_Step] | None = Field(
        default=None,
        description="Optional ordered list of steps to perform after the initial navigation. Each step is {action, value}.",
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
        default=_DEFAULT_WAIT_TIMEOUT_MS, ge=1000, le=120000, description="Timeout applied to each 'wait_for' step."
    )


class _Outputs(BaseModel):
    session_id: str = Field(description="Browser session handle for downstream browser.* nodes.")
    final_url: str = Field(description="The page URL after all steps ran.")
    title: str = Field(description="The page title after all steps ran (may be empty).")


SPEC = CapabilitySpec(
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
    final_url, title = fallback_url, ""
    try:
        result = await session.evaluate(_HREF_JS)
        if isinstance(result, str) and result:
            final_url = result
    except Exception:  # pragma: no cover - best-effort
        logger.debug("cap.web_navigate could not read location.href", exc_info=True)
    try:
        result = await session.evaluate(_TITLE_JS)
        if isinstance(result, str):
            title = result
    except Exception:  # pragma: no cover - best-effort
        logger.debug("cap.web_navigate could not read document.title", exc_info=True)
    return final_url, title


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    url = inputs["url"]
    raw_steps = inputs.get("steps") or []
    session_id = inputs.get("session_id")
    wait_timeout = int(inputs.get("wait_timeout_ms", _DEFAULT_WAIT_TIMEOUT_MS))

    steps: list[dict[str, str]] = []
    for s in raw_steps:
        if isinstance(s, _Step):
            steps.append({"action": s.action, "value": s.value})
        else:
            steps.append({"action": s["action"], "value": s["value"]})

    logger.info("cap.web_navigate start run_id=%s url=%s reuse=%s steps=%d", ctx.run_id, url, bool(session_id), len(steps))

    fresh_cm = None
    if session_id:
        session = get_session(ctx.session_state, session_id)
        await session.navigate(url)
    else:
        if ctx.browser_pool is None:
            raise CapabilityError("cap.web_navigate requires a browser_pool when no session_id is given")
        fresh_cm = ctx.browser_pool.checkout()
        session = await fresh_cm.__aenter__()
        try:
            await session.navigate(url)
        except Exception:
            await fresh_cm.__aexit__(None, None, None)
            raise

    try:
        for step in steps:
            action, value = step["action"], step["value"]
            if action == "goto":
                await session.navigate(value)
            elif action == "wait_for":
                await session.wait_for(value, timeout_ms=wait_timeout)
            elif action == "click":
                await session.click(value)
            else:  # pragma: no cover - guarded by the Literal
                raise ValueError(f"cap.web_navigate: unknown step action {action!r}")
    except Exception:
        if fresh_cm is not None:
            await fresh_cm.__aexit__(None, None, None)
        raise

    if fresh_cm is not None:
        ctx.session_state[stash_key(session.id)] = SessionHolder(cm=fresh_cm, session=session)

    final_url, title = await _read_page_state(session, fallback_url=url)
    logger.info("cap.web_navigate ok run_id=%s session=%s final_url=%s", ctx.run_id, session.id, final_url)
    return {"session_id": session.id, "final_url": final_url, "title": title}
