"""cap.web_login — log into an arbitrary web application.

Drives a fresh browser session through a generic username/password login form
and returns an authenticated session handle for downstream nodes.

Shared capability: the SAME code runs on the server and on a remote agent. It
touches only the portable CapabilityContext surface:
  - ``browser_pool`` / ``session_state`` — the live session (local on whichever host),
  - ``secrets`` — username/password (server: from the vault; agent: shipped in
    the dispatch envelope),
  - ``complete_text`` — optional LLM selector disambiguation (proxied to the
    server's LLM; the agent never holds the OpenAI key),
  - ``open_signal`` — captcha/MFA human-in-the-loop (proxied to the server's
    signal hub; the human answers in the same chat UI wherever the browser runs),
  - ``write_object`` — stores the captcha image (canonical store on the server).
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aakaar_caps.browser.state import SessionHolder, stash_key
from aakaar_caps.caps.web_login.discovery import LoginFormDescriptor, discover_login_form
from aakaar_caps.context import CapabilityContext, CapabilityError
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.web_login"

_DEFAULT_TIMEOUT_MS = 15000


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_alias: str = Field(
        description="Which credential set to use, e.g. 'primary'. The grant must exist."
    )
    login_url: str | None = Field(
        default=None,
        description=(
            "URL of the login page. Usually supplied by the grant's "
            "`input_defaults` — leave this null and the executor injects "
            "the per-tenant URL at run time. Set explicitly only to "
            "override the grant's URL."
        ),
    )
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
    def _check_captcha_pair(self) -> _Inputs:
        a = self.captcha_image_selector
        b = self.captcha_input_selector
        if bool(a) != bool(b):
            raise ValueError(
                "captcha_image_selector and captcha_input_selector must be set together"
            )
        return self


class _Outputs(BaseModel):
    session: str = Field(description="Browser session handle for downstream browser.* nodes.")


SPEC = CapabilitySpec(
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
        ("username", "Account username (or email)."),
        ("password", "Account password."),
    ),
    tags=("auth", "browser"),
    stateful_session=True,
)

_CAPTCHA_PROMPT_TIMEOUT_S = 300


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    if ctx.browser_pool is None:
        raise CapabilityError("cap.web_login requires a browser_pool")

    alias = inputs["account_alias"]
    logger.info("cap.web_login start run_id=%s alias=%s url=%s", ctx.run_id, alias, inputs.get("login_url"))
    creds = ctx.secrets
    if "username" not in creds or "password" not in creds:
        raise PermissionError(
            f"cap.web_login: no username/password for alias {alias!r} (grant missing or not shipped)"
        )

    timeout = int(inputs.get("timeout_ms", _DEFAULT_TIMEOUT_MS))
    success_selector = inputs.get("success_selector")
    explicit_username = inputs.get("username_selector")
    explicit_password = inputs.get("password_selector")
    explicit_submit = inputs.get("submit_selector")
    explicit_captcha_image = inputs.get("captcha_image_selector")
    explicit_captcha_input = inputs.get("captcha_input_selector")

    login_url = inputs.get("login_url")
    if not login_url:
        logger.warning("cap.web_login: missing login_url for alias=%s", alias)
        raise RuntimeError(
            f"cap.web_login: no login_url for alias {alias!r}; "
            "set login_url on the grant's input_defaults (Vault → edit site → URL)"
        )

    cm = ctx.browser_pool.checkout()
    session = await cm.__aenter__()
    try:
        await session.navigate(login_url)

        need_discovery = not (explicit_username and explicit_password and explicit_submit)
        descriptor: LoginFormDescriptor | None = None
        if need_discovery:
            descriptor = await discover_login_form(session)
            if not descriptor.password_selector and not explicit_password:
                raise RuntimeError(
                    f"cap.web_login could not find a login form on {login_url!r}; "
                    f"discovery reasons: {descriptor.ambiguity_reasons}"
                )
            if descriptor.ambiguity_reasons and ctx.text_completer is not None:
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
                ctx, session=session, image_selector=captcha_image_selector, timeout_ms=timeout
            )
            await session.fill(captcha_input_selector, captcha_value)
        elif descriptor and descriptor.captcha_kind in ("recaptcha", "hcaptcha", "turnstile"):
            await _pause_for_third_party_captcha(ctx, descriptor.captcha_kind)

        await session.click(submit_selector)
        if success_selector:
            await session.wait_for(success_selector, timeout_ms=timeout)
        else:
            await session.wait_for(username_selector, timeout_ms=timeout, state="detached")
    except Exception:
        await cm.__aexit__(None, None, None)
        raise

    ctx.session_state[stash_key(session.id)] = SessionHolder(cm=cm, session=session)
    logger.info("cap.web_login ok run_id=%s alias=%s session=%s", ctx.run_id, alias, session.id)
    return {"session": session.id}


# ---------- HITL helpers --------------------------------------------------


async def _solve_captcha_via_human(
    ctx: CapabilityContext, *, session: Any, image_selector: str, timeout_ms: int
) -> str:
    """Capture the captcha image, hand it to the user via the HITL channel, and
    return the typed answer."""
    await session.wait_for(image_selector, timeout_ms=timeout_ms)
    image_bytes = await session.screenshot_element(image_selector)
    key = f"runs/{ctx.run_id}/captcha/{ctx.node_id}_{uuid.uuid4().hex}.png"
    uri = await ctx.write_object(key, image_bytes)
    return await ctx.open_signal(f"Solve the captcha shown at {uri}", "text")


async def _pause_for_third_party_captcha(ctx: CapabilityContext, kind: str) -> None:
    """Pause for a third-party challenge (recaptcha/hcaptcha/turnstile) solved
    by a human in the (headed) browser; any reply means 'continue'."""
    await ctx.open_signal(
        f"This page uses {kind}. Solve it in the browser window, then reply "
        "'done' here so the run can proceed with submit.",
        "confirm",
    )


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
    ctx: CapabilityContext, descriptor: LoginFormDescriptor
) -> LoginFormDescriptor | None:
    """Ask the LLM (via the portable free-text seam) for selector tiebreaks. The
    seam returns free text; we parse the JSON object out of it. Returns a refined
    descriptor on success, or None — callers fall back to the heuristics.

    Uses ``complete_text`` (not ``complete_plan``): the planner seam forces the
    reply through the workflow ``PlannerCompletion`` envelope, which requires a
    ``kind`` field and forbids the bare selector keys this prompt asks for, so
    every reply was rejected and disambiguation silently never ran. The text
    seam round-trips to the same server LLM with no envelope, on both the server
    and a remote agent (see ``ctx.complete_text`` / back-channel ``llm_complete``)."""
    snapshot = descriptor.form_outer_html_excerpt or ""
    if not snapshot:
        return None
    user_prompt = _DISAMBIGUATE_PROMPT.format(
        form_html=snapshot,
        username=descriptor.username_selector,
        password=descriptor.password_selector,
        submit=descriptor.submit_selector,
        captcha_image=descriptor.captcha_image_selector,
        captcha_input=descriptor.captcha_input_selector,
        reasons=", ".join(descriptor.ambiguity_reasons) or "(none)",
    )
    system_prompt = (
        "You are a CSS-selector assistant. Reply with one JSON object, "
        "no markdown fences, no prose. Use the simplest stable selector "
        "(prefer #id, then [name=...], then class). All five keys must "
        "be present; use null for captcha keys when no captcha exists."
    )
    try:
        raw = (
            await asyncio.to_thread(ctx.complete_text, system_prompt, user_prompt) or ""
        ).strip()
    except Exception:  # noqa: BLE001
        return None
    if not raw or not raw.startswith("{"):
        return None
    try:
        parsed = _json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(parsed, dict):
        return None
    return LoginFormDescriptor(
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
