"""cap.screenshot — capture a screenshot of the current page (or a single
element) and store it in managed storage.

Operates on an existing session — chain after `cap.open_url`, `cap.web_login`,
or any node that hands back a session handle. Returns the managed-storage
URI of the resulting PNG.

Shared capability: the SAME code runs on the server and on a remote agent. It
programs only against the portable CapabilityContext (browser session via
session_state, bytes via write_object).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar_caps.browser.state import get_session
from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.screenshot"

_DEFAULT_TIMEOUT_MS = 15000


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str = Field(
        description=(
            "Browser session handle from an upstream node (e.g. "
            "${open.session} or ${login.session})."
        )
    )
    selector: str | None = Field(
        default=None,
        description=(
            "Optional CSS selector — when set, only that element is captured "
            "instead of the whole page. Useful for snapshotting a single chart "
            "or table."
        ),
    )
    wait_selector: str | None = Field(
        default=None,
        description=(
            "Optional CSS selector to wait for before snapping. When omitted "
            "and `selector` is set, the handler waits for `selector` itself."
        ),
    )
    timeout_ms: int = Field(
        default=_DEFAULT_TIMEOUT_MS,
        ge=1000,
        le=120000,
        description="Selector wait timeout.",
    )


class _Outputs(BaseModel):
    image_uri: str = Field(
        description="Managed-storage URI of the captured PNG (`aakaar://...`)."
    )


SPEC = CapabilitySpec(
    ref=CAP_REF,
    description=(
        "Capture a screenshot of the current page (or a single element when "
        "`selector` is supplied) using an existing browser session, and store "
        "the PNG in managed storage. Optionally waits for a selector first."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("browser", "screenshot"),
)


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = get_session(ctx.session_state, inputs["session"])
    selector = inputs.get("selector")
    wait_selector = inputs.get("wait_selector") or selector
    timeout = int(inputs.get("timeout_ms", _DEFAULT_TIMEOUT_MS))

    logger.info(
        "cap.screenshot start run_id=%s session=%s selector=%r wait=%r",
        ctx.run_id, inputs["session"], selector, wait_selector,
    )

    if wait_selector:
        await sess.wait_for(wait_selector, timeout_ms=timeout)

    if selector:
        image = await sess.screenshot_element(selector)
    else:
        image = await sess.screenshot()

    key = f"runs/{ctx.run_id}/screenshots/{uuid.uuid4().hex}.png"
    uri = await ctx.write_object(key, image)
    logger.info("cap.screenshot ok run_id=%s uri=%s bytes=%d", ctx.run_id, uri, len(image))
    return {"image_uri": uri}
