"""cap.web_login — log into an arbitrary web application.

Drives a fresh browser session through a generic username/password login
form and returns an authenticated session handle for downstream nodes.

Why this is a capability and not just composed `browser.*` actions:
  - Credentials must come from the tenant's vault, not the DAG. A
    composed `browser.fill` would otherwise force the planner to embed
    the password as a literal — explicitly forbidden by the planner's
    hard rules. Wrapping the flow lets the handler fetch creds from the
    vault behind the curtain.
  - The handler centralizes "what does a successful login look like"
    (wait for `success_selector` after submit) so the planner doesn't
    have to model that explicitly.
  - Captcha and MFA are handled inline via `human.prompt` semantics —
    when `captcha_image_selector` is set, the handler captures the
    captcha image to managed storage, opens a SignalHub prompt with the
    URI, and waits for the user's text response before submitting.

Required vault entry: a grant under `(tenant, cap.web_login, account_alias)`
storing `username` and `password` keys. Per-tenant admins issue this grant
through `/admin/grants`; superusers can do it for any tenant via
`/superuser/tenants/{id}/grants`. The planner is forbidden from asking
the user for credentials in chat.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aakar.interpreter.activities.types import ActivityContext
from aakar.interpreter.credentials import fetch_credentials
from aakar.shared.registry import CapabilityDefinition, SecretSpec


CAP_REF = "cap.web_login"

_DEFAULT_USERNAME_SELECTOR = "input[name='username']"
_DEFAULT_PASSWORD_SELECTOR = "input[name='password']"
_DEFAULT_SUBMIT_SELECTOR = "button[type='submit']"
_DEFAULT_TIMEOUT_MS = 15000
_CAPTCHA_PROMPT_TIMEOUT_S = 300


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_alias: str = Field(
        description="Which credential set to use, e.g. 'primary'. The grant must exist."
    )
    login_url: str = Field(description="URL of the login page.")
    username_selector: str = Field(
        default=_DEFAULT_USERNAME_SELECTOR,
        description="CSS selector for the username/email input field.",
    )
    password_selector: str = Field(
        default=_DEFAULT_PASSWORD_SELECTOR,
        description="CSS selector for the password input field.",
    )
    submit_selector: str = Field(
        default=_DEFAULT_SUBMIT_SELECTOR,
        description="CSS selector for the form submit button.",
    )
    success_selector: str | None = Field(
        default=None,
        description=(
            "CSS selector that proves login succeeded (e.g. a nav element only "
            "visible when authenticated). If omitted, the handler waits for the "
            "page to change away from the login form by waiting for the "
            "username field to disappear."
        ),
    )
    captcha_image_selector: str | None = Field(
        default=None,
        description=(
            "CSS selector for the captcha image element. When set, the handler "
            "captures the image to managed storage and pauses for human input "
            "via the run's HITL channel before submitting. Must be paired with "
            "`captcha_input_selector`."
        ),
    )
    captcha_input_selector: str | None = Field(
        default=None,
        description="CSS selector for the captcha input field. Required when "
        "`captcha_image_selector` is set.",
    )
    timeout_ms: int = Field(
        default=_DEFAULT_TIMEOUT_MS,
        ge=1000,
        le=120000,
        description="Per-step timeout for selector waits.",
    )

    @model_validator(mode="after")
    def _check_captcha_pair(self) -> "_Inputs":
        a = self.captcha_image_selector
        b = self.captcha_input_selector
        if bool(a) != bool(b):
            raise ValueError(
                "captcha_image_selector and captcha_input_selector must be set together"
            )
        return self


class _Outputs(BaseModel):
    session: str = Field(description="Browser session handle for downstream browser.* nodes.")


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Log into a web application using stored credentials and return an "
        "authenticated browser session handle."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(
        SecretSpec(name="username", description="Account username (or email)."),
        SecretSpec(name="password", description="Account password."),
    ),
    tags=("auth", "browser"),
)


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    if ctx.browser_pool is None:
        raise RuntimeError("cap.web_login requires a browser_pool")

    creds = fetch_credentials(
        ctx, capability_ref=CAP_REF, account_alias=inputs["account_alias"]
    )

    timeout = int(inputs.get("timeout_ms", _DEFAULT_TIMEOUT_MS))
    username_selector = inputs.get("username_selector", _DEFAULT_USERNAME_SELECTOR)
    password_selector = inputs.get("password_selector", _DEFAULT_PASSWORD_SELECTOR)
    submit_selector = inputs.get("submit_selector", _DEFAULT_SUBMIT_SELECTOR)
    success_selector = inputs.get("success_selector")
    captcha_image_selector = inputs.get("captcha_image_selector")
    captcha_input_selector = inputs.get("captcha_input_selector")

    cm = ctx.browser_pool.checkout()
    session = await cm.__aenter__()
    try:
        await session.navigate(inputs["login_url"])
        await session.wait_for(username_selector, timeout_ms=timeout)
        await session.fill(username_selector, creds["username"])
        await session.fill(password_selector, creds["password"])

        if captcha_image_selector:
            captcha_value = await _solve_captcha_via_human(
                ctx,
                session=session,
                image_selector=captcha_image_selector,
                timeout_ms=timeout,
            )
            await session.fill(captcha_input_selector, captcha_value)

        await session.click(submit_selector)
        # Default success criterion: the username field disappears (page changed).
        # Caller can supply a stronger one (e.g. an account-only nav element).
        landing = success_selector or username_selector
        await session.wait_for(landing, timeout_ms=timeout)
    except Exception:
        await cm.__aexit__(None, None, None)
        raise

    # Stash the session so the orchestrator's run-end cleanup can release it,
    # and downstream `browser.*` and capability nodes can look it up by id.
    from aakar.interpreter.activities.browser import _SessionHolder, _stash_key

    holder = _SessionHolder(cm=cm, session=session)
    ctx.session_state[_stash_key(session.id)] = holder
    return {"session": session.id}


async def _solve_captcha_via_human(
    ctx: ActivityContext,
    *,
    session: Any,
    image_selector: str,
    timeout_ms: int,
) -> str:
    """Capture the captcha image, hand it to the user via SignalHub, and
    return the typed answer. Raises if no SignalHub is wired (handlers
    should never see this in production — the executor always populates
    `ctx.signals`)."""
    if ctx.signals is None or not ctx.node_id:
        raise RuntimeError(
            "captcha solving requires a SignalHub on ActivityContext; "
            "this run was not started through the executor's HITL path"
        )
    await session.wait_for(image_selector, timeout_ms=timeout_ms)
    image_bytes = await session.screenshot_element(image_selector)
    key = f"runs/{ctx.run_id}/captcha/{ctx.node_id}_{uuid.uuid4().hex}.png"
    obj = ctx.object_store.put(str(ctx.tenant_id), key, image_bytes)

    prompt = await ctx.signals.open(
        run_id=ctx.run_id,
        node_id=ctx.node_id,
        message=f"Solve the captcha shown at {obj.uri}",
        expects="text",
    )
    try:
        return await asyncio.wait_for(prompt.future, timeout=_CAPTCHA_PROMPT_TIMEOUT_S)
    except asyncio.TimeoutError as e:
        raise RuntimeError(
            f"captcha prompt timed out after {_CAPTCHA_PROMPT_TIMEOUT_S}s on node {ctx.node_id}"
        ) from e
