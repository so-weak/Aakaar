"""cap.open_url — open a fresh browser session and navigate to a URL.

Self-contained entry-point capability: takes a URL (no credentials, no
upstream session), checks out a browser from the pool, navigates, and
returns the session handle so downstream nodes can use the same page.

Shared capability: identical code on the server and a remote agent — it uses
the pool/session_state on whichever host runs it.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar_caps.browser.state import SessionHolder, stash_key
from aakaar_caps.context import CapabilityContext, CapabilityError
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.open_url"

_DEFAULT_TIMEOUT_MS = 15000


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(description="Absolute URL to navigate to (http or https).")
    wait_selector: str | None = Field(
        default=None,
        description=(
            "Optional CSS selector that must appear after navigation before "
            "the capability returns. Use this when the page renders content "
            "via JS and the run shouldn't continue until the DOM is ready."
        ),
    )
    timeout_ms: int = Field(
        default=_DEFAULT_TIMEOUT_MS,
        ge=1000,
        le=120000,
        description="Selector wait timeout (only consulted when wait_selector is set).",
    )


class _Outputs(BaseModel):
    session: str = Field(description="Browser session handle for downstream browser.* nodes.")
    url: str = Field(description="The URL that was navigated to (echo).")


SPEC = CapabilitySpec(
    ref=CAP_REF,
    description=(
        "Open a fresh browser session and navigate to a public URL (no login). "
        "Optionally waits for a selector to appear before returning. Returns "
        "the session handle so downstream nodes can interact with the same page."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("browser", "navigate"),
)


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    if ctx.browser_pool is None:
        raise CapabilityError("cap.open_url requires a browser_pool")

    url = inputs["url"]
    wait_selector = inputs.get("wait_selector")
    timeout = int(inputs.get("timeout_ms", _DEFAULT_TIMEOUT_MS))
    logger.info("cap.open_url start run_id=%s url=%s wait_selector=%r", ctx.run_id, url, wait_selector)

    cm = ctx.browser_pool.checkout()
    session = await cm.__aenter__()
    try:
        await session.navigate(url)
        if wait_selector:
            await session.wait_for(wait_selector, timeout_ms=timeout)
    except Exception:
        await cm.__aexit__(None, None, None)
        raise

    ctx.session_state[stash_key(session.id)] = SessionHolder(cm=cm, session=session)
    logger.info("cap.open_url ok run_id=%s session=%s", ctx.run_id, session.id)
    return {"session": session.id, "url": url}
