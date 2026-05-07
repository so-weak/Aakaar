"""System prompt assembler.

The prompt encodes Aakar's hard rules for the planner, then enumerates only
the refs the LLM is allowed to use:
  - capabilities granted to the current tenant
  - all generic action and control primitives

It deliberately does NOT include capability *implementations* or any secret
material. The LLM sees ref names, descriptions, input/output field shapes,
and the secret-name list (so it knows credentials are auto-injected and
doesn't try to ask the user for them).

`build_messages` is the single entry point that produces the message list
for one planner round-trip. The repair loop reuses the same builder and
appends a tool-error message on retries.
"""

from __future__ import annotations

import json
import typing
from dataclasses import dataclass
from typing import Any, Union

from pydantic.fields import FieldInfo

from aakar.planner.llm import LLMMessage, Role
from aakar.shared.dag.types import Dag
from aakar.shared.registry import (
    ActionDefinition,
    CapabilityDefinition,
    ControlDefinition,
    Registry,
    SecretSpec,
)
from aakar.shared.registry.types import Definition


@dataclass(slots=True)
class PromptBuilder:
    """Builds a planner prompt for one tenant + one user turn.

    Construct once per request with the tenant's grants; call `build_messages`
    with the user's NL message and (optionally) the current DAG for edits.
    """

    registry: Registry
    granted_capabilities: set[str]

    def build_messages(
        self,
        user_message: str,
        *,
        current_dag: Dag | None = None,
        chat_history: list[LLMMessage] | None = None,
        repair_errors: list[str] | None = None,
    ) -> list[LLMMessage]:
        system = self._system_prompt(current_dag=current_dag)
        messages: list[LLMMessage] = [LLMMessage(role=Role.SYSTEM, content=system)]
        if chat_history:
            messages.extend(chat_history)
        messages.append(LLMMessage(role=Role.USER, content=user_message))
        if repair_errors:
            messages.append(
                LLMMessage(
                    role=Role.USER,
                    content=_repair_message(repair_errors),
                )
            )
        return messages

    # --- internals ---------------------------------------------------------

    def _system_prompt(self, *, current_dag: Dag | None) -> str:
        granted_caps = [
            d
            for d in self.registry.capabilities()
            if d.ref in self.granted_capabilities
        ]
        actions = self.registry.actions()
        controls = self.registry.controls()

        sections = [
            _HEADER,
            _RULES,
            _RESPONSE_ENVELOPE,
            _DAG_SHAPE,
            "# Available capabilities (granted to this tenant)",
            _capabilities_block(granted_caps),
            "# Available action primitives",
            _definitions_block(actions),
            "# Available control nodes",
            _definitions_block(controls),
        ]
        if current_dag is not None:
            sections.append("# Current workflow (you are editing this DAG)")
            sections.append("```json")
            sections.append(json.dumps(current_dag.model_dump(by_alias=True), indent=2))
            sections.append("```")
        return "\n\n".join(sections).rstrip() + "\n"


# ---------- prompt blocks --------------------------------------------------


_HEADER = (
    "You are the Aakar workflow planner. Convert the user's natural-language "
    "request into a workflow DAG that the Aakar runtime will execute, OR ask "
    "for clarification, OR report that no granted capability can fulfill the "
    "request. You never execute anything — you only plan."
)


_RULES = """\
# Hard rules — you MUST follow these

- Use ONLY the refs listed below under "Available capabilities", "Available action primitives", and "Available control nodes". Do not invent refs.
- NEVER ask for usernames, passwords, OTPs, API keys, or any other secrets. Credentials are stored alongside the capability; the user only chooses an `account_alias` (e.g. "primary"). If the relevant capability isn't granted to this tenant, respond with `kind: "missing"` and tell the user to set up a grant via the admin grants UI (`/admin/grants` for tenant admins, or via the superuser tenant detail page) — do not ask the user to type credentials in chat.
- DO ask for everything else that's missing. URLs, CSS selectors, which file to upload, what to download, page-specific names of fields, success markers — these are NOT secrets and you must collect them via `kind: "clarify"` if the user's request is incomplete. Ask one specific question per missing input; do not ask multi-part open-ended questions.
- Reference upstream node outputs with `${node_id.field}` strings (or `${alias.field}` if a node sets `outputs_as`). Do not embed runtime data in the DAG.
- Use `cap.web_login` for any authenticated browser flow. Chain it to `cap.file_download` / `cap.file_upload` / `browser.*` actions via `${login.session}`. If the user mentions a captcha on the login page, set `cap.web_login`'s `captcha_image_selector` and `captcha_input_selector` inputs — the handler captures the captcha image and pauses for the user via the run's HITL channel. Do NOT use `human.prompt` for captchas; the capability handles it inline. If the user mentions an OTP / MFA step *after* login, compose a `human.prompt` (expects: "otp") between the login node and the next action; for filling that OTP into a form, use `browser.fill_secret` only if the value is in the vault, otherwise use `browser.fill` with the prompt's `${response}`.
- Respond with exactly one `kind`:
  - `dag` — a complete, valid DAG you are confident will execute.
  - `clarify` — one or more specific questions to disambiguate the request. Prefer this when in doubt.
  - `missing` — capability refs that would unblock the request, plus an explanation. List refs that don't exist yet too; staff use these signals to author new capabilities.
- Always set a brief `rationale` (one or two sentences for the chat UI)."""


_RESPONSE_ENVELOPE = """\
# Response shape

Reply with ONLY a single JSON object — no prose, no markdown, no code fences.
The object must conform to this schema:

```
{
  "kind": "dag" | "clarify" | "missing",   // pick exactly one
  "rationale": "<one or two sentences for the chat UI>",
  "dag":         { ...DAG... } | null,     // required iff kind == "dag"; otherwise null
  "questions":   ["..."] | [],             // required (non-empty) iff kind == "clarify"; otherwise []
  "needed":      ["cap.foo", ...] | [],    // required (non-empty) iff kind == "missing"; otherwise []
  "explanation": "<plain English>" | ""    // required (non-empty) iff kind == "missing"; otherwise ""
}
```

Always include every key. Use `null`/`[]`/`""` for the branches that don't apply.
Do not add any keys outside this set."""


_DAG_SHAPE = """\
# DAG shape

- A DAG is `{ "id": "", "version": 0, "nodes": [...], "edges": [...] }`. Leave `id` empty and `version` 0 — the workflow service assigns them on save.
- Each node: `{ "id", "kind", "ref", "inputs", "outputs_as" }`. `id` is short alphanumeric/underscore. `kind` is `capability`, `action`, or `control` (must match the ref's registered kind).
- Edges: `{ "from": <id>, "to": <id> }`. No cycles. Independent nodes run in parallel.
- Inputs may carry literal JSON values or `${ref}` strings. A `${ref}` must occupy the entire string value — no embedding in larger strings."""


def _capabilities_block(caps: list[CapabilityDefinition]) -> str:
    if not caps:
        return (
            "_No capabilities are granted to this tenant._ "
            "If the request needs site-specific automation, respond with `kind: \"missing\"` "
            "naming the capability ref(s) you would need."
        )
    return "\n\n".join(_describe_capability(c) for c in caps)


def _definitions_block(defs: list[ActionDefinition] | list[ControlDefinition]) -> str:
    if not defs:
        return "_(none)_"
    return "\n\n".join(_describe_definition(d) for d in defs)


def _describe_capability(cap: CapabilityDefinition) -> str:
    block = _describe_definition(cap)
    if cap.secrets:
        secrets_line = ", ".join(_describe_secret(s) for s in cap.secrets)
        block += f"\n  secrets (auto-injected, do NOT ask the user): {secrets_line}"
    return block


def _describe_secret(s: SecretSpec) -> str:
    return f"`{s.name}`{f' — {s.description}' if s.description else ''}"


def _describe_definition(defn: Definition) -> str:
    lines = [f"- `{defn.ref}` ({defn.kind.value}): {defn.description}"]
    inputs = list(defn.input_schema.model_fields.items())
    if inputs:
        lines.append("  inputs:")
        for name, info in inputs:
            lines.append("    " + _describe_field(name, info))
    else:
        lines.append("  inputs: (none)")
    outputs = list(defn.output_schema.model_fields.items())
    if outputs:
        lines.append("  outputs:")
        for name, info in outputs:
            lines.append("    " + _describe_field(name, info))
    else:
        lines.append("  outputs: (none)")
    return "\n".join(lines)


def _describe_field(name: str, info: FieldInfo) -> str:
    type_label = _annotation_label(info.annotation)
    required = "required" if info.is_required() else "optional"
    desc = info.description or ""
    suffix = f" — {desc}" if desc else ""
    return f"`{name}` ({type_label}, {required}){suffix}"


def _annotation_label(annotation: Any) -> str:
    if annotation is None or annotation is type(None):  # noqa: E721
        return "null"
    origin = typing.get_origin(annotation)
    if origin is None:
        if annotation is str:
            return "string"
        if annotation is int:
            return "integer"
        if annotation is bool:
            return "boolean"
        if annotation is float:
            return "number"
        if isinstance(annotation, type):
            return annotation.__name__
        return str(annotation)
    args = typing.get_args(annotation)
    if origin is list:
        return f"array<{_annotation_label(args[0])}>" if args else "array"
    if origin is dict:
        return "object"
    if origin in (Union, typing.Union):
        return " | ".join(_annotation_label(a) for a in args)
    if origin is typing.Literal:
        return " | ".join(repr(a) for a in args)
    return str(annotation)


# ---------- repair feedback ------------------------------------------------


def _repair_message(errors: list[str]) -> str:
    bulleted = "\n".join(f"- {e}" for e in errors)
    return (
        "Your previous response did not validate. Fix and respond again "
        "following the same hard rules. Do NOT apologize or explain — just "
        "produce a corrected response.\n\nValidation errors:\n" + bulleted
    )
