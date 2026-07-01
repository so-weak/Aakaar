"""Plan-time tools the agentic planner can call.

Each tool has:
  - `name` — what the LLM calls.
  - `schema` — OpenAI function-calling schema sent in the request.
  - `dispatch(runner, args)` — async callable that runs the tool and
    returns a JSON-serializable result the LLM consumes.

The runner owns the Playwright session + tenant context (vault, grants).
Tools NEVER mutate persistent state and are explicitly read-only with
two exceptions: `login_with_grant` (drives a real browser login, but
only with vault-stored creds for the tenant's own grants) and `done`
(records the final DAG so the loop can exit). No tools post data, write
files, or persist anything outside the per-plan browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aakaar.planner.agentic.runner import PlannerToolRunner


# OpenAI function-calling schema for each tool. The LLM sees these and
# decides which to call. We keep schemas small + concrete; the model is
# better at picking from short option lists than reasoning over big
# schemas.

INSPECT_PAGE_TOOL = {
    "name": "inspect_page",
    "description": (
        "Return a structured snapshot of the current page: URL, title, "
        "the first ~2 KB of visible text, and a list of interactive "
        "elements (links, buttons, inputs, selects) with stable CSS "
        "selectors and their visible labels. Use this BEFORE making any "
        "selector decisions in the final DAG. The result is read-only; "
        "calling inspect_page does not change the page."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


NAVIGATE_TOOL = {
    "name": "navigate",
    "description": (
        "Navigate the planning browser to a URL. Use this to load the "
        "user's target site or move between pages. Returns the final URL "
        "and page title after the navigation settles."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute URL to load."}
        },
        "required": ["url"],
        "additionalProperties": False,
    },
}


CLICK_TOOL = {
    "name": "click",
    "description": (
        "Click an element on the *current* page to reveal what happens next, "
        "then return a fresh snapshot (same shape as inspect_page) so you can "
        "see fields, buttons, or modals that only appear after the click. Use "
        "this when the request talks about something AFTER a click ('after "
        "clicking Continue, enter the date') — clicking + re-inspecting is how "
        "you discover field labels that aren't on the landing page, so you "
        "never have to ask the user. SAFE plan-time clicks: OK, Cancel, "
        "Continue, Next, Back, Show, View, Refresh, Search, Filter, accordions/"
        "disclosure toggles, and tab switchers. DO NOT click Submit, Pay, Send, "
        "Place Order, Delete, Confirm, Logout, or anything that commits a "
        "transaction or destroys state — those belong only in the final DAG at "
        "execute time. Prefer `text` over `selector`; the runner uses the same "
        "robust label matcher as `browser.click_by_text`."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "Visible label / button caption / link text to click. "
                    "Case-insensitive. Preferred input."
                ),
            },
            "selector": {
                "type": "string",
                "description": (
                    "CSS selector — only when you already have an exact one "
                    "from a prior inspect_page result."
                ),
            },
        },
        "additionalProperties": False,
    },
}


LOGIN_TOOL = {
    "name": "login_with_grant",
    "description": (
        "Log into a site at `login_url` using credentials stored under the "
        "tenant's `cap.web_login` grant for `account_alias`. Selectors are "
        "auto-discovered. The tool refuses if the grant doesn't exist, so "
        "you don't need to ask the user for credentials. Returns the URL "
        "the page lands on after submit."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "login_url": {"type": "string"},
            "account_alias": {
                "type": "string",
                "description": "Defaults to 'primary' if the user didn't say.",
            },
        },
        "required": ["login_url"],
        "additionalProperties": False,
    },
}


DONE_TOOL = {
    "name": "done",
    "description": (
        "Emit the final workflow DAG and stop. Call this exactly once "
        "when you have enough information to write the DAG. The DAG must "
        "use ONLY refs from the registry (capabilities granted to this "
        "tenant + universal action and control primitives). For "
        "cap.file_download nodes, prefer the `target_hint` input (the "
        "natural-language report name) over guessing a selector when "
        "you couldn't reach the post-login page (e.g. it had a captcha) — "
        "the capability resolves the hint at run time."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["dag", "clarify", "missing"]},
            "rationale": {"type": "string"},
            "dag": {
                "type": ["object", "null"],
                "description": (
                    "Required when kind=='dag'. Same shape as the standard "
                    "PlannerCompletion.dag: {id:'', version:0, nodes:[...], edges:[...]}."
                ),
            },
            "questions": {"type": "array", "items": {"type": "string"}},
            "needed": {"type": "array", "items": {"type": "string"}},
            "explanation": {"type": "string"},
        },
        "required": ["kind", "rationale"],
        "additionalProperties": False,
    },
}


def all_tool_schemas() -> list[dict[str, Any]]:
    return [INSPECT_PAGE_TOOL, NAVIGATE_TOOL, CLICK_TOOL, LOGIN_TOOL, DONE_TOOL]


# ---------- dispatch ----------------------------------------------------------


@dataclass(slots=True)
class ToolResult:
    """Serialized tool result to feed back to the LLM. The runner stores
    these and also formats them as `tool` role messages on the next turn."""

    name: str
    payload: dict[str, Any]


async def dispatch(
    runner: PlannerToolRunner, name: str, args: dict[str, Any]
) -> ToolResult:
    if name == "inspect_page":
        return ToolResult(name=name, payload=await runner.inspect_page())
    if name == "navigate":
        url = args.get("url")
        if not isinstance(url, str) or not url:
            return ToolResult(name=name, payload={"error": "url is required"})
        return ToolResult(name=name, payload=await runner.navigate(url))
    if name == "click":
        text = args.get("text")
        selector = args.get("selector")
        return ToolResult(
            name=name,
            payload=await runner.click(
                text=text if isinstance(text, str) and text else None,
                selector=selector if isinstance(selector, str) and selector else None,
            ),
        )
    if name == "login_with_grant":
        login_url = args.get("login_url")
        if not isinstance(login_url, str) or not login_url:
            return ToolResult(name=name, payload={"error": "login_url is required"})
        alias = args.get("account_alias") or "primary"
        return ToolResult(
            name=name,
            payload=await runner.login_with_grant(login_url=login_url, account_alias=alias),
        )
    if name == "done":
        return ToolResult(name=name, payload={"_done": True, **args})
    return ToolResult(name=name, payload={"error": f"unknown tool: {name}"})
