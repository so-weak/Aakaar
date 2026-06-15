"""cap.web_form_fill — fill (and optionally submit) a web form.

Drives a browser session through a list of `(selector, value)` pairs,
filling each field in order, then optionally clicks a submit control.

Two entry modes, exactly one of which must be supplied:
  - `url`: open a *fresh* browser session, navigate to the URL, then
    fill the form. The new session is stashed so downstream nodes can
    reuse it (the returned `session_id`).
  - `session_id`: reuse a session opened by an upstream node
    (`cap.open_url`, `cap.web_login`, `browser.open_session`, ...).
    Use this to fill a form on a page the run already navigated to —
    e.g. after logging in.

Why a capability and not raw `browser.fill` + `browser.click`:
  - Keeps multi-field "fill this form" intents to a single DAG node, so
    the planner doesn't have to emit one node per field.
  - Centralizes the "fill all, then submit, then report counts" pattern
    and the success/echo contract downstream nodes branch on.

Secrets: NONE. This capability fills only non-secret literal values that
the planner places directly in the DAG. To inject a credential into a
field, use `browser.fill_secret` (which reads from the vault) — never
put a secret in `fields[].value`.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import CapabilityDefinition

logger = logging.getLogger(__name__)
CAP_REF = "cap.web_form_fill"

_DEFAULT_TIMEOUT_MS = 15000


class _Field(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selector: str = Field(description="CSS selector for the form control to fill.")
    value: str = Field(
        description=(
            "Non-secret literal value to type into the field. Never place a "
            "credential here — use browser.fill_secret for vault-backed values."
        )
    )


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str | None = Field(
        default=None,
        description=(
            "Absolute URL to open in a fresh browser session before filling. "
            "Provide either `url` or `session_id`, not both."
        ),
    )
    session_id: str | None = Field(
        default=None,
        description=(
            "Handle of an existing browser session (from cap.open_url, "
            "cap.web_login, browser.open_session, ...). Provide either "
            "`session_id` or `url`, not both."
        ),
    )
    fields: list[_Field] = Field(
        min_length=1,
        description="Ordered list of {selector, value} pairs to fill. Non-secret only.",
    )
    submit_selector: str | None = Field(
        default=None,
        description=(
            "Optional CSS selector for the submit control. When set, the "
            "capability clicks it after filling every field."
        ),
    )
    wait_each: bool = Field(
        default=True,
        description=(
            "Wait for each field's selector to be present before filling it. "
            "Set false to skip the waits (faster, but flakier on JS-rendered forms)."
        ),
    )
    timeout_ms: int = Field(
        default=_DEFAULT_TIMEOUT_MS,
        ge=1000,
        le=120000,
        description="Per-field selector wait timeout (consulted when wait_each is true).",
    )


class _Outputs(BaseModel):
    session_id: str = Field(
        description="Browser session handle (the fresh one or the reused one)."
    )
    filled: int = Field(description="Number of fields successfully filled.")
    submitted: bool = Field(description="Whether the submit control was clicked.")


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Fill a web form's fields (a list of {selector, value} pairs) and "
        "optionally submit it. Either opens a fresh session at `url` or reuses "
        "an existing `session_id`. Non-secret values only; returns the session "
        "handle, the number of fields filled, and whether it submitted."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("browser", "form"),
)


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    if ctx.browser_pool is None:
        raise RuntimeError("cap.web_form_fill requires a browser_pool")

    url = inputs.get("url")
    session_id = inputs.get("session_id")
    if bool(url) == bool(session_id):
        raise ValueError(
            "cap.web_form_fill: provide exactly one of `url` or `session_id`"
        )

    fields = inputs["fields"]
    submit_selector = inputs.get("submit_selector")
    wait_each = bool(inputs.get("wait_each", True))
    timeout = int(inputs.get("timeout_ms", _DEFAULT_TIMEOUT_MS))

    logger.info(
        "cap.web_form_fill start run_id=%s mode=%s fields=%d submit=%s",
        ctx.run_id,
        "url" if url else "session",
        len(fields),
        bool(submit_selector),
    )

    if url:
        return await _fill_fresh_session(
            ctx,
            url=url,
            fields=fields,
            submit_selector=submit_selector,
            wait_each=wait_each,
            timeout=timeout,
        )
    return await _fill_existing_session(
        ctx,
        session_id=str(session_id),
        fields=fields,
        submit_selector=submit_selector,
        wait_each=wait_each,
        timeout=timeout,
    )


async def _fill_fresh_session(
    ctx: ActivityContext,
    *,
    url: str,
    fields: list[dict[str, Any]],
    submit_selector: str | None,
    wait_each: bool,
    timeout: int,
) -> dict[str, Any]:
    cm = ctx.browser_pool.checkout()
    session = await cm.__aenter__()
    try:
        await session.navigate(url)
        filled, submitted = await _drive_form(
            session,
            fields=fields,
            submit_selector=submit_selector,
            wait_each=wait_each,
            timeout=timeout,
        )
    except Exception:
        # New session we own — release it so a failure doesn't leak a worker.
        await cm.__aexit__(None, None, None)
        raise

    from aakaar.interpreter.activities.browser import _SessionHolder, _stash_key

    holder = _SessionHolder(cm=cm, session=session)
    ctx.session_state[_stash_key(session.id)] = holder
    logger.info(
        "cap.web_form_fill ok run_id=%s session=%s filled=%d submitted=%s",
        ctx.run_id,
        session.id,
        filled,
        submitted,
    )
    return {"session_id": session.id, "filled": filled, "submitted": submitted}


async def _fill_existing_session(
    ctx: ActivityContext,
    *,
    session_id: str,
    fields: list[dict[str, Any]],
    submit_selector: str | None,
    wait_each: bool,
    timeout: int,
) -> dict[str, Any]:
    # Reuse an upstream session. We do NOT own its checkout, so on failure
    # we leave it open for the run-end cleanup / a downstream retry.
    from aakaar.interpreter.activities.browser import _get_session

    session = _get_session(ctx, session_id)
    filled, submitted = await _drive_form(
        session,
        fields=fields,
        submit_selector=submit_selector,
        wait_each=wait_each,
        timeout=timeout,
    )
    logger.info(
        "cap.web_form_fill ok run_id=%s session=%s filled=%d submitted=%s",
        ctx.run_id,
        session_id,
        filled,
        submitted,
    )
    return {"session_id": session_id, "filled": filled, "submitted": submitted}


async def _drive_form(
    session: Any,
    *,
    fields: list[dict[str, Any]],
    submit_selector: str | None,
    wait_each: bool,
    timeout: int,
) -> tuple[int, bool]:
    """Fill every field in order, then optionally click submit. Returns
    (filled_count, submitted)."""
    filled = 0
    for field in fields:
        selector = field["selector"]
        value = field["value"]
        if wait_each:
            await session.wait_for(selector, timeout_ms=timeout)
        await session.fill(selector, value)
        filled += 1

    submitted = False
    if submit_selector:
        await session.click(submit_selector)
        submitted = True
    return filled, submitted
