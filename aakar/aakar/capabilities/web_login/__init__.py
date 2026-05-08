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
    when a captcha is detected (either supplied via inputs or auto-found
    by `discover_login_form`), the handler captures the captcha image
    to managed storage, opens a SignalHub prompt with the URI, and
    waits for the user's text response before submitting.

Self-discovery: when the user does not supply `username_selector` /
`password_selector` / `submit_selector`, the handler runs a JS-based
DOM walk on the live login page (see `discovery.py`) and uses whatever
it finds. Captcha widgets are detected by the same pass and trigger an
HITL pause automatically — the user does not need to know there's a
captcha. If the discovery is ambiguous and an LLM is wired into the
ActivityContext, the handler asks the LLM to disambiguate from a DOM
snapshot (a runtime-only, narrow use of the LLM, distinct from
planning).

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

from aakar.capabilities.web_login.discovery import (
    LoginFormDescriptor,
    discover_login_form,
)
from aakar.interpreter.activities.types import ActivityContext
from aakar.interpreter.credentials import fetch_credentials
from aakar.shared.registry import CapabilityDefinition, SecretSpec


CAP_REF = "cap.web_login"

_DEFAULT_TIMEOUT_MS = 15000
_CAPTCHA_PROMPT_TIMEOUT_S = 300


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_alias: str = Field(
        description="Which credential set to use, e.g. 'primary'. The grant must exist."
    )
    login_url: str = Field(description="URL of the login page.")
    username_selector: str | None = Field(
        default=None,
        description=(
            "CSS selector for the username/email input. Optional — if omitted, "
            "the handler discovers it by inspecting the page after navigation."
        ),
    )
    password_selector: str | None = Field(
        default=None,
        description=(
            "CSS selector for the password input. Optional — auto-discovered "
            "from the page when not supplied."
        ),
    )
    submit_selector: str | None = Field(
        default=None,
        description=(
            "CSS selector for the form submit control. Optional — auto-"
            "discovered from the page when not supplied."
        ),
    )
    success_selector: str | None = Field(
        default=None,
        description=(
            "CSS selector that proves login succeeded (e.g. a nav element only "
            "visible when authenticated). If omitted, the handler waits for the "
            "username input to disappear from the DOM."
        ),
    )
    captcha_image_selector: str | None = Field(
        default=None,
        description=(
            "CSS selector for an image-captcha element. Optional — the handler "
            "will auto-detect common captcha widgets (image, recaptcha, hcaptcha, "
            "turnstile) during discovery. Set this only if the page has a custom "
            "image-captcha that auto-detection misses."
        ),
    )
    captcha_input_selector: str | None = Field(
        default=None,
        description=(
            "CSS selector for the captcha input field. Required only when "
            "`captcha_image_selector` is explicitly set."
        ),
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
        "authenticated browser session handle. Selectors are auto-discovered "
        "if not supplied; captcha widgets (image / recaptcha / hcaptcha / "
        "turnstile) are detected and handled via the run's HITL channel."
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
    success_selector = inputs.get("success_selector")
    explicit_username = inputs.get("username_selector")
    explicit_password = inputs.get("password_selector")
    explicit_submit = inputs.get("submit_selector")
    explicit_captcha_image = inputs.get("captcha_image_selector")
    explicit_captcha_input = inputs.get("captcha_input_selector")

    cm = ctx.browser_pool.checkout()
    session = await cm.__aenter__()
    try:
        await session.navigate(inputs["login_url"])

        # Selector resolution: prefer caller-supplied selectors. If any of
        # the three core ones is missing, run discovery once — it's cheap
        # (one JS evaluation) and produces both the missing core selectors
        # and the captcha annotation.
        need_discovery = not (explicit_username and explicit_password and explicit_submit)
        descriptor: LoginFormDescriptor | None = None
        if need_discovery:
            descriptor = await discover_login_form(session)
            if not descriptor.password_selector and not explicit_password:
                # Without a password input there's no login form. Surface a
                # specific failure rather than blindly retrying selectors.
                raise RuntimeError(
                    f"cap.web_login could not find a login form on {inputs['login_url']!r}; "
                    f"discovery reasons: {descriptor.ambiguity_reasons}"
                )
            if descriptor.ambiguity_reasons and ctx.llm is not None:
                resolved = await _llm_disambiguate(ctx, descriptor)
                if resolved:
                    descriptor = resolved

        username_selector = explicit_username or (descriptor and descriptor.username_selector)
        password_selector = explicit_password or (descriptor and descriptor.password_selector)
        submit_selector = explicit_submit or (descriptor and descriptor.submit_selector)
        if not username_selector or not password_selector or not submit_selector:
            raise RuntimeError(
                "cap.web_login could not resolve all of (username, password, submit) "
                f"selectors; discovery={descriptor!r}"
            )

        # Captcha resolution: explicit pair wins; otherwise honor whatever
        # discovery surfaced. Currently we only handle image captchas
        # automatically; recaptcha / hcaptcha / turnstile pause for human
        # input but cannot be solved with a simple text response (Phase 2).
        captcha_image_selector = explicit_captcha_image or (
            descriptor.captcha_image_selector if descriptor else None
        )
        captcha_input_selector = explicit_captcha_input or (
            descriptor.captcha_input_selector if descriptor else None
        )

        await session.wait_for(username_selector, timeout_ms=timeout)
        await session.fill(username_selector, creds["username"])
        await session.fill(password_selector, creds["password"])

        if captcha_image_selector and captcha_input_selector:
            captcha_value = await _solve_captcha_via_human(
                ctx,
                session=session,
                image_selector=captcha_image_selector,
                timeout_ms=timeout,
            )
            await session.fill(captcha_input_selector, captcha_value)
        elif descriptor and descriptor.captcha_kind in ("recaptcha", "hcaptcha", "turnstile"):
            # Third-party challenge frames — pause the run so a human can
            # solve them in the same browser context. We don't have a
            # solved-token to inject; we just wait for the user to confirm.
            await _pause_for_third_party_captcha(ctx, descriptor.captcha_kind, timeout)

        await session.click(submit_selector)
        # Success criterion:
        #  - if the caller supplied `success_selector`, wait for it to
        #    appear (a positive marker of post-login UI).
        #  - otherwise wait for the username field to *detach* — i.e. the
        #    page actually navigated away from the login form. Using the
        #    default 'attached' state would falsely succeed when the
        #    server re-renders the form after a wrong-credential submit.
        if success_selector:
            await session.wait_for(success_selector, timeout_ms=timeout)
        else:
            await session.wait_for(
                username_selector, timeout_ms=timeout, state="detached"
            )
    except Exception:
        await cm.__aexit__(None, None, None)
        raise

    from aakar.interpreter.activities.browser import _SessionHolder, _stash_key

    holder = _SessionHolder(cm=cm, session=session)
    ctx.session_state[_stash_key(session.id)] = holder
    return {"session": session.id}


# ---------- HITL helpers --------------------------------------------------


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


async def _pause_for_third_party_captcha(
    ctx: ActivityContext, kind: str, _timeout_ms: int
) -> None:
    """Open an HITL prompt asking the user to solve a third-party captcha
    (recaptcha / hcaptcha / turnstile) in the (headed) browser. The
    handler doesn't actually need the user's text — submitting any
    response signals 'I solved it, continue'."""
    if ctx.signals is None or not ctx.node_id:
        raise RuntimeError(
            f"a {kind} challenge was detected, but no SignalHub is wired"
        )
    prompt = await ctx.signals.open(
        run_id=ctx.run_id,
        node_id=ctx.node_id,
        message=(
            f"This page uses {kind}. Solve it in the browser window, then reply "
            "'done' here so the run can proceed with submit."
        ),
        expects="confirm",
    )
    try:
        await asyncio.wait_for(prompt.future, timeout=_CAPTCHA_PROMPT_TIMEOUT_S)
    except asyncio.TimeoutError as e:
        raise RuntimeError(
            f"{kind} prompt timed out after {_CAPTCHA_PROMPT_TIMEOUT_S}s on node {ctx.node_id}"
        ) from e


# ---------- LLM-fallback disambiguation -----------------------------------


_DISAMBIGUATE_PROMPT = """\
You are a DOM introspection assistant. Given the outerHTML of a login form
and the current best-guess selectors (which had ambiguity), reply with the
single most-likely set of CSS selectors as JSON. Do NOT explain.

Reply schema (no extra keys, all values must be CSS selectors):
{{
  "username_selector": "...",
  "password_selector": "...",
  "submit_selector": "...",
  "captcha_image_selector": "..." | null,
  "captcha_input_selector": "..." | null
}}

Form HTML:
```
{form_html}
```

Best-guess selectors (may be wrong; you can override):
- username_selector: {username}
- password_selector: {password}
- submit_selector: {submit}
- captcha_image_selector: {captcha_image}
- captcha_input_selector: {captcha_input}

Ambiguity reasons reported by heuristics: {reasons}
"""


async def _llm_disambiguate(
    ctx: ActivityContext, descriptor: LoginFormDescriptor
) -> LoginFormDescriptor | None:
    """Ask the planner LLM for selector tiebreaks. Returns a refined
    descriptor on success, or None if the LLM call or parse failed —
    callers fall back to whatever heuristics produced."""
    from aakar.planner.llm import LLMMessage, Role

    snapshot = descriptor.form_outer_html_excerpt or ""
    if not snapshot:
        return None
    prompt = _DISAMBIGUATE_PROMPT.format(
        form_html=snapshot,
        username=descriptor.username_selector,
        password=descriptor.password_selector,
        submit=descriptor.submit_selector,
        captcha_image=descriptor.captcha_image_selector,
        captcha_input=descriptor.captcha_input_selector,
        reasons=", ".join(descriptor.ambiguity_reasons) or "(none)",
    )
    messages = [
        LLMMessage(
            role=Role.SYSTEM,
            content=(
                "You are a CSS-selector assistant. Reply with one JSON object, "
                "no markdown fences, no prose. Use the simplest stable selector "
                "(prefer #id, then [name=...], then class). All five keys must "
                "be present; use null for captcha keys when no captcha exists."
            ),
        ),
        LLMMessage(role=Role.USER, content=prompt),
    ]
    try:
        completion = await asyncio.to_thread(ctx.llm.complete_planner, messages)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return None
    # The planner LLM is constrained to PlannerCompletion shape, so we
    # piggy-back on its `rationale` field — that's where free-form text
    # lands. JSON-parse the rationale and pull selectors out of it.
    import json as _json

    raw = (completion.rationale or "").strip()
    if not raw or not raw.startswith("{"):
        return None
    try:
        parsed = _json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(parsed, dict):
        return None
    refined = LoginFormDescriptor(
        ok=True,
        ambiguity_reasons=descriptor.ambiguity_reasons + ["llm_disambiguated"],
        username_selector=parsed.get("username_selector") or descriptor.username_selector,
        password_selector=parsed.get("password_selector") or descriptor.password_selector,
        submit_selector=parsed.get("submit_selector") or descriptor.submit_selector,
        captcha_image_selector=parsed.get("captcha_image_selector") or descriptor.captcha_image_selector,
        captcha_input_selector=parsed.get("captcha_input_selector") or descriptor.captcha_input_selector,
        captcha_kind=descriptor.captcha_kind,
        form_outer_html_excerpt=descriptor.form_outer_html_excerpt,
    )
    return refined
