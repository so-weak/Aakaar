"""Shared helpers for capability tests that need a scripted login-form
discovery result.

The FakeBrowserSession's `evaluate()` matches programmed responses by
substring of the JS payload — `DISCOVERY_MARKER` is something present in
the real discovery JS so the fake returns our canned descriptor.
"""

from __future__ import annotations

from typing import Any


# Substring present in the real discovery JS (`bestSelector` is one of
# the helper functions). Stable enough to use as a match key.
DISCOVERY_MARKER = "bestSelector"


def discovery_response(
    *,
    username: str = "input[name='username']",
    password: str = "input[name='password']",
    submit: str = "button[type='submit']",
    captcha_image: str | None = None,
    captcha_input: str | None = None,
    captcha_kind: str | None = None,
    ambiguity_reasons: list[str] | None = None,
    form_html: str = "<form>...</form>",
) -> dict[str, dict[str, Any]]:
    """Build an `evaluate_responses` mapping that returns a scripted login-
    form descriptor when `cap.web_login`'s discovery JS runs.

    Pass straight into `FakeBrowserSession(evaluate_responses=...)`.
    """
    return {
        DISCOVERY_MARKER: {
            "ok": True,
            "ambiguity_reasons": ambiguity_reasons or [],
            "username_selector": username,
            "password_selector": password,
            "submit_selector": submit,
            "captcha_image_selector": captcha_image,
            "captcha_input_selector": captcha_input,
            "captcha_kind": captcha_kind,
            "form_outer_html_excerpt": form_html,
        }
    }
