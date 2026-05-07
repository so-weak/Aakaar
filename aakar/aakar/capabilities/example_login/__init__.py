"""cap.example_login — sample capability against a fictional `example.test`.

Demonstrates the full capability author workflow:
  1. Open a browser session.
  2. Navigate to a login URL.
  3. Pull credentials from the vault by alias (NOT from the user).
  4. Fill the form, click submit, wait for landing.
  5. Return the session id so downstream nodes can use the authenticated browser.

There is no real `example.test` site — this capability is exercised by
tests with a `FakeBrowserSession` that records the calls and returns
canned responses. A future production capability would look almost
identical, just with real selectors and a live URL.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakar.capabilities._base import fetch_credentials
from aakar.interpreter.activities.types import ActivityContext
from aakar.shared.registry import CapabilityDefinition, SecretSpec


CAP_REF = "cap.example_login"


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_alias: str = Field(description="Which credential set to use, e.g. 'primary'.")


class _Outputs(BaseModel):
    session: str = Field(description="Browser session handle for downstream browser.* nodes.")


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Log into the example.test portal and return an authenticated browser "
        "session handle."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(
        SecretSpec(name="username", description="Account username."),
        SecretSpec(name="password", description="Account password."),
    ),
    tags=("auth", "demo"),
)


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    if ctx.browser_pool is None:
        raise RuntimeError("cap.example_login requires a browser_pool")
    creds = fetch_credentials(
        ctx, capability_ref=CAP_REF, account_alias=inputs["account_alias"]
    )

    cm = ctx.browser_pool.checkout()
    session = await cm.__aenter__()
    try:
        await session.navigate("https://example.test/login")
        await session.wait_for("input[name='username']", timeout_ms=10000)
        await session.fill("input[name='username']", creds["username"])
        await session.fill("input[name='password']", creds["password"])
        await session.click("button[type='submit']")
        await session.wait_for("nav[aria-label='Account']", timeout_ms=10000)
    except Exception:
        await cm.__aexit__(None, None, None)
        raise

    # Hand the session to the orchestrator's cleanup via session_state, the
    # same way browser.open_session does. We import locally to avoid a
    # circular dependency with the activities package.
    from aakar.interpreter.activities.browser import _SessionHolder, _stash_key

    holder = _SessionHolder(cm=cm, session=session)
    ctx.session_state[_stash_key(session.id)] = holder
    return {"session": session.id}
