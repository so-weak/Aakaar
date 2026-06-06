"""cap.email_parse — turn an email/text body into structured data via the runtime LLM.

This is a server-local, read-only extraction capability. It takes a chunk
of free text (typically an email body the upstream graph already fetched),
an instruction describing what to pull out, and a `mode` selecting the
shape of the answer:

  - "kv":      key/value pairs (the default). Returns {"data": {<k>: <v>, ...}}.
  - "table":   a list of row objects. Returns {"data": [{...}, {...}, ...]}.
  - "summary": a short natural-language summary. Returns {"text": "..."}.

The handler builds a small system+user prompt and asks
`ctx.llm.complete_text(system, user)` for a JSON answer, then parses it.
The model is used only for narrow, read-only extraction on
already-fetched data — never for action selection — so this stays on the
right side of the planner/executor spine.

Graceful degradation, per the capability rules:
  - `ctx.llm is None`, or the model returns empty / unparseable output:
    we fall back to a deterministic heuristic instead of failing the node.
    For "kv" we run a naive `Key: value` line scanner over the body; for
    "table" we return an empty list; for "summary" we return the raw text
    (truncated). The fallback never raises on well-formed input.

No secrets, no network, no files. Output is one of {"data": ...} (kv/table)
or {"text": ...} (summary), plus a `used_llm` flag so downstream nodes /
audit can tell whether extraction was model-backed or heuristic.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import CapabilityDefinition

logger = logging.getLogger(__name__)
CAP_REF = "cap.email_parse"

_DEFAULT_INSTRUCTION = "Extract the salient fields from the message."
_SUMMARY_FALLBACK_CHARS = 2000

# Naive "Key: value" line scanner used when no LLM is available. We accept a
# leading dash/bullet, a label of word-ish characters/spaces, then a colon.
_KV_LINE = re.compile(r"^\s*[-*]?\s*([A-Za-z][\w \-/]{0,60}?)\s*:\s*(.+?)\s*$")


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(
        description="The message/email body (already fetched) to parse.",
    )
    instruction: str = Field(
        default=_DEFAULT_INSTRUCTION,
        description=(
            "What to extract, in plain language, e.g. 'pull the invoice "
            "number, amount and due date'. Guides the LLM; ignored by the "
            "heuristic fallback."
        ),
    )
    mode: Literal["kv", "table", "summary"] = Field(
        default="kv",
        description=(
            "Output shape: 'kv' -> {data: {key: value}}, 'table' -> "
            "{data: [row, ...]}, 'summary' -> {text: ...}."
        ),
    )


class _Outputs(BaseModel):
    # Exactly one of `data` / `text` is populated depending on `mode`.
    data: Any = Field(
        default=None,
        description="Structured result for kv (object) or table (list of rows).",
    )
    text: str | None = Field(
        default=None,
        description="Natural-language result for summary mode.",
    )
    used_llm: bool = Field(
        description="True when the result came from the LLM; False when the "
        "deterministic fallback produced it.",
    )


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Parse a text/email body into structured data using the runtime LLM. "
        "Modes: 'kv' returns key/value pairs, 'table' returns a list of rows, "
        "'summary' returns a short text summary. Falls back to a deterministic "
        "heuristic when no LLM is configured or the model output is unusable. "
        "Read-only extraction on already-fetched content; no network, no files."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("comms", "email", "extract", "llm", "parse"),
)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _build_prompts(text: str, instruction: str, mode: str) -> tuple[str, str]:
    """Return (system, user) prompts for the requested mode.

    The system prompt pins the model to JSON-only output in a fixed shape so
    `_parse_llm_json` can be strict; the user prompt carries the instruction
    and the body.
    """
    if mode == "table":
        shape = (
            'a JSON object {"rows": [ {..}, {..} ]} where each row is an object '
            "of column-name -> value. Use consistent keys across rows."
        )
    elif mode == "summary":
        shape = 'a JSON object {"summary": "<one short paragraph>"}.'
    else:  # kv
        shape = (
            'a JSON object {"fields": {"<field>": "<value>"}} mapping each '
            "extracted field to its value. Values may be strings, numbers, "
            "booleans, or null."
        )
    system = (
        "You extract structured data from a message body. Respond with ONLY "
        "valid JSON and nothing else (no prose, no code fences). The JSON must be "
        + shape
        + " If a requested field is absent, omit it (kv/table) or note it in the "
        "summary. Do not invent values."
    )
    user = f"Instruction: {instruction}\n\n--- MESSAGE BODY START ---\n{text}\n--- MESSAGE BODY END ---"
    return system, user


# ---------------------------------------------------------------------------
# LLM output parsing
# ---------------------------------------------------------------------------


def _strip_code_fence(raw: str) -> str:
    """Tolerate a model that wraps JSON in ```json ... ``` despite instructions."""
    s = raw.strip()
    if s.startswith("```"):
        # Drop the opening fence line (``` or ```json) and the trailing fence.
        s = s[3:]
        if "\n" in s:
            first, rest = s.split("\n", 1)
            if first.strip().lower() in {"", "json"}:
                s = rest
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip()


def _loads_lenient(raw: str) -> Any:
    """Parse JSON, falling back to the first {...} or [...] block in the text."""
    s = _strip_code_fence(raw)
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        pass
    # Last resort: grab the first balanced-looking object/array span.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = s.find(opener)
        end = s.rfind(closer)
        if 0 <= start < end:
            try:
                return json.loads(s[start : end + 1])
            except (json.JSONDecodeError, ValueError):
                continue
    raise ValueError("LLM output was not parseable JSON")


def _parse_llm_json(raw: str, mode: str) -> dict[str, Any] | None:
    """Map the model's JSON onto the capability's output shape.

    Returns the output dict on success, or None if the output is empty or
    cannot be coerced into the expected shape (the caller then falls back).
    """
    if not raw or not raw.strip():
        return None
    try:
        parsed = _loads_lenient(raw)
    except ValueError:
        logger.info("cap.email_parse: LLM output not parseable as JSON; falling back")
        return None

    if mode == "kv":
        fields = parsed.get("fields") if isinstance(parsed, dict) else None
        if fields is None and isinstance(parsed, dict):
            # Model returned a bare object instead of {"fields": {...}}.
            fields = parsed
        if not isinstance(fields, dict):
            return None
        return {"data": fields, "text": None, "used_llm": True}

    if mode == "table":
        rows = parsed.get("rows") if isinstance(parsed, dict) else parsed
        if not isinstance(rows, list):
            return None
        rows = [r for r in rows if isinstance(r, dict)]
        return {"data": rows, "text": None, "used_llm": True}

    # summary
    summary: Any = None
    if isinstance(parsed, dict):
        summary = parsed.get("summary")
    if summary is None and isinstance(parsed, str):
        summary = parsed
    if not isinstance(summary, str) or not summary.strip():
        return None
    return {"data": None, "text": summary.strip(), "used_llm": True}


# ---------------------------------------------------------------------------
# Deterministic fallbacks (no LLM)
# ---------------------------------------------------------------------------


def _naive_kv(text: str) -> dict[str, str]:
    """Scan `Key: value` lines into a dict. First occurrence of a key wins."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _KV_LINE.match(line)
        if not m:
            continue
        key = m.group(1).strip()
        val = m.group(2).strip()
        if key and key not in out:
            out[key] = val
    return out


def _fallback(text: str, mode: str) -> dict[str, Any]:
    if mode == "table":
        return {"data": [], "text": None, "used_llm": False}
    if mode == "summary":
        trimmed = text.strip()
        if len(trimmed) > _SUMMARY_FALLBACK_CHARS:
            trimmed = trimmed[:_SUMMARY_FALLBACK_CHARS].rstrip() + "..."
        return {"data": None, "text": trimmed, "used_llm": False}
    return {"data": _naive_kv(text), "text": None, "used_llm": False}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    text = inputs["text"]
    instruction = (inputs.get("instruction") or _DEFAULT_INSTRUCTION).strip()
    mode = inputs.get("mode") or "kv"

    if ctx.llm is None:
        logger.info(
            "cap.email_parse run_id=%s mode=%s: no llm configured; using heuristic",
            ctx.run_id,
            mode,
        )
        return _fallback(text, mode)

    system, user = _build_prompts(text, instruction, mode)
    try:
        raw = ctx.llm.complete_text(system, user)
    except Exception:
        # The model call is best-effort: a transient LLM error should not
        # fail the node when a deterministic answer is available.
        logger.warning(
            "cap.email_parse run_id=%s mode=%s: llm.complete_text raised; "
            "falling back to heuristic",
            ctx.run_id,
            mode,
            exc_info=True,
        )
        return _fallback(text, mode)

    result = _parse_llm_json(raw or "", mode)
    if result is None:
        logger.info(
            "cap.email_parse run_id=%s mode=%s: empty/unusable llm output; "
            "using heuristic",
            ctx.run_id,
            mode,
        )
        return _fallback(text, mode)

    logger.info("cap.email_parse run_id=%s mode=%s ok (llm)", ctx.run_id, mode)
    return result
