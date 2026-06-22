"""cap.web_form_fill — fill (and optionally submit) a web form.

Fills a list of (selector, value) pairs, then optionally submits. Either opens
a fresh session at `url` or reuses an existing `session_id`. Shared: identical
on the server and a remote agent. Non-secret values only — use
browser.fill_secret for vault-backed values.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar_caps.browser.state import SessionHolder, get_session, stash_key
from aakaar_caps.context import CapabilityContext, CapabilityError
from aakaar_caps.spec import CapabilitySpec

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
    session_id: str = Field(description="Browser session handle (the fresh one or the reused one).")
    filled: int = Field(description="Number of fields successfully filled.")
    submitted: bool = Field(description="Whether the submit control was clicked.")


SPEC = CapabilitySpec(
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


async def _drive_form(session: Any, *, fields: list[dict[str, Any]], submit_selector: str | None, wait_each: bool, timeout: int) -> tuple[int, bool]:
    filled = 0
    for field in fields:
        if wait_each:
            await session.wait_for(field["selector"], timeout_ms=timeout)
        await session.fill(field["selector"], field["value"])
        filled += 1
    submitted = False
    if submit_selector:
        await session.click(submit_selector)
        submitted = True
    return filled, submitted


def _as_field_dicts(fields: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in fields:
        if isinstance(f, _Field):
            out.append({"selector": f.selector, "value": f.value})
        else:
            out.append({"selector": f["selector"], "value": f["value"]})
    return out


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    if ctx.browser_pool is None:
        raise CapabilityError("cap.web_form_fill requires a browser_pool")

    url = inputs.get("url")
    session_id = inputs.get("session_id")
    if bool(url) == bool(session_id):
        raise ValueError("cap.web_form_fill: provide exactly one of `url` or `session_id`")

    fields = _as_field_dicts(inputs["fields"])
    submit_selector = inputs.get("submit_selector")
    wait_each = bool(inputs.get("wait_each", True))
    timeout = int(inputs.get("timeout_ms", _DEFAULT_TIMEOUT_MS))

    logger.info("cap.web_form_fill start run_id=%s mode=%s fields=%d", ctx.run_id, "url" if url else "session", len(fields))

    if url:
        cm = ctx.browser_pool.checkout()
        session = await cm.__aenter__()
        try:
            await session.navigate(url)
            filled, submitted = await _drive_form(
                session, fields=fields, submit_selector=submit_selector, wait_each=wait_each, timeout=timeout
            )
        except Exception:
            await cm.__aexit__(None, None, None)
            raise
        ctx.session_state[stash_key(session.id)] = SessionHolder(cm=cm, session=session)
        return {"session_id": session.id, "filled": filled, "submitted": submitted}

    # Reuse an upstream session — we do NOT own its checkout.
    session = get_session(ctx.session_state, str(session_id))
    filled, submitted = await _drive_form(
        session, fields=fields, submit_selector=submit_selector, wait_each=wait_each, timeout=timeout
    )
    return {"session_id": session_id, "filled": filled, "submitted": submitted}
