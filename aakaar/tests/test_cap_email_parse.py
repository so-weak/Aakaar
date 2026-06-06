"""Tests for cap.email_parse.

Drives the handler with a hand-built ActivityContext and a FakeLLMClient,
covering:
  - kv / table / summary happy paths (LLM-backed)
  - code-fence tolerance and bare-object coercion
  - heuristic fallbacks when llm is None / returns empty / raises / is junk
  - definition shape (registered ref, no secrets)
  - the pure helpers (_naive_kv, _strip_code_fence, _loads_lenient)
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from aakaar.capabilities.comms.email_parse import (
    CAP_REF,
    _loads_lenient,
    _naive_kv,
    _strip_code_fence,
    definition,
    handler,
)
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.planner.llm import FakeLLMClient


def _ctx(tmp_path: Path, llm: Any) -> ActivityContext:
    from aakaar.shared.registry import build_default_registry
    from aakaar.storage import LocalFsObjectStore
    from aakaar.vault import LocalVault

    return ActivityContext(
        tenant_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        registry=build_default_registry(),
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=LocalVault(tmp_path / "vault"),
        llm=llm,
    )


_BODY = (
    "Hello team,\n"
    "Invoice Number: INV-42\n"
    "Amount: 1500.00\n"
    "Due Date: 2026-07-01\n"
    "Thanks."
)


# --------------------------------------------------------------------------
# LLM happy paths
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kv_mode_uses_llm_json(tmp_path: Path) -> None:
    reply = json.dumps({"fields": {"invoice": "INV-42", "amount": 1500.0}})
    llm = FakeLLMClient(text_replies=[reply])
    ctx = _ctx(tmp_path, llm)

    out = await handler(ctx, {"text": _BODY, "instruction": "pull invoice", "mode": "kv"})

    assert out["used_llm"] is True
    assert out["text"] is None
    assert out["data"] == {"invoice": "INV-42", "amount": 1500.0}
    # The body was actually handed to the model.
    assert _BODY in llm.text_calls[0][1]


@pytest.mark.asyncio
async def test_kv_mode_defaults_when_mode_omitted(tmp_path: Path) -> None:
    llm = FakeLLMClient(text_replies=[json.dumps({"fields": {"k": "v"}})])
    ctx = _ctx(tmp_path, llm)
    out = await handler(ctx, {"text": _BODY})
    assert out["data"] == {"k": "v"}
    assert out["used_llm"] is True


@pytest.mark.asyncio
async def test_kv_mode_coerces_bare_object(tmp_path: Path) -> None:
    """Model returned {...} directly instead of {'fields': {...}}."""
    llm = FakeLLMClient(text_replies=[json.dumps({"a": 1, "b": 2})])
    ctx = _ctx(tmp_path, llm)
    out = await handler(ctx, {"text": _BODY, "mode": "kv"})
    assert out["data"] == {"a": 1, "b": 2}
    assert out["used_llm"] is True


@pytest.mark.asyncio
async def test_table_mode_returns_rows(tmp_path: Path) -> None:
    reply = json.dumps({"rows": [{"item": "a", "qty": 1}, {"item": "b", "qty": 2}]})
    llm = FakeLLMClient(text_replies=[reply])
    ctx = _ctx(tmp_path, llm)
    out = await handler(ctx, {"text": _BODY, "mode": "table"})
    assert out["used_llm"] is True
    assert out["text"] is None
    assert out["data"] == [{"item": "a", "qty": 1}, {"item": "b", "qty": 2}]


@pytest.mark.asyncio
async def test_table_mode_drops_non_dict_rows(tmp_path: Path) -> None:
    reply = json.dumps({"rows": [{"x": 1}, "junk", 5]})
    llm = FakeLLMClient(text_replies=[reply])
    ctx = _ctx(tmp_path, llm)
    out = await handler(ctx, {"text": _BODY, "mode": "table"})
    assert out["data"] == [{"x": 1}]


@pytest.mark.asyncio
async def test_summary_mode_returns_text(tmp_path: Path) -> None:
    reply = json.dumps({"summary": "An invoice INV-42 for 1500 due 2026-07-01."})
    llm = FakeLLMClient(text_replies=[reply])
    ctx = _ctx(tmp_path, llm)
    out = await handler(ctx, {"text": _BODY, "mode": "summary"})
    assert out["used_llm"] is True
    assert out["data"] is None
    assert "INV-42" in out["text"]


@pytest.mark.asyncio
async def test_code_fence_is_tolerated(tmp_path: Path) -> None:
    reply = "```json\n" + json.dumps({"fields": {"k": "v"}}) + "\n```"
    llm = FakeLLMClient(text_replies=[reply])
    ctx = _ctx(tmp_path, llm)
    out = await handler(ctx, {"text": _BODY, "mode": "kv"})
    assert out["data"] == {"k": "v"}
    assert out["used_llm"] is True


@pytest.mark.asyncio
async def test_json_embedded_in_prose_is_extracted(tmp_path: Path) -> None:
    reply = 'Sure! Here you go: {"fields": {"k": "v"}} hope that helps'
    llm = FakeLLMClient(text_replies=[reply])
    ctx = _ctx(tmp_path, llm)
    out = await handler(ctx, {"text": _BODY, "mode": "kv"})
    assert out["data"] == {"k": "v"}
    assert out["used_llm"] is True


# --------------------------------------------------------------------------
# Fallbacks
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_llm_falls_back_to_naive_kv(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, llm=None)
    out = await handler(ctx, {"text": _BODY, "mode": "kv"})
    assert out["used_llm"] is False
    assert out["data"]["Invoice Number"] == "INV-42"
    assert out["data"]["Amount"] == "1500.00"
    assert out["data"]["Due Date"] == "2026-07-01"


@pytest.mark.asyncio
async def test_empty_llm_output_falls_back(tmp_path: Path) -> None:
    # FakeLLMClient with no queued replies returns "" -> heuristic.
    ctx = _ctx(tmp_path, FakeLLMClient())
    out = await handler(ctx, {"text": _BODY, "mode": "kv"})
    assert out["used_llm"] is False
    assert out["data"]["Amount"] == "1500.00"


@pytest.mark.asyncio
async def test_unparseable_llm_output_falls_back(tmp_path: Path) -> None:
    llm = FakeLLMClient(text_replies=["this is not json at all"])
    ctx = _ctx(tmp_path, llm)
    out = await handler(ctx, {"text": _BODY, "mode": "kv"})
    assert out["used_llm"] is False
    assert out["data"]["Invoice Number"] == "INV-42"


@pytest.mark.asyncio
async def test_llm_exception_falls_back(tmp_path: Path) -> None:
    class _BoomLLM:
        def complete_text(self, system: str, user: str) -> str:
            raise RuntimeError("rate limited")

    ctx = _ctx(tmp_path, _BoomLLM())
    out = await handler(ctx, {"text": _BODY, "mode": "kv"})
    assert out["used_llm"] is False
    assert out["data"]["Amount"] == "1500.00"


@pytest.mark.asyncio
async def test_table_fallback_is_empty_list(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, llm=None)
    out = await handler(ctx, {"text": _BODY, "mode": "table"})
    assert out["used_llm"] is False
    assert out["data"] == []
    assert out["text"] is None


@pytest.mark.asyncio
async def test_summary_fallback_returns_raw_text(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, llm=None)
    out = await handler(ctx, {"text": "  short body  ", "mode": "summary"})
    assert out["used_llm"] is False
    assert out["text"] == "short body"
    assert out["data"] is None


@pytest.mark.asyncio
async def test_summary_fallback_truncates_long_text(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, llm=None)
    body = "x" * 5000
    out = await handler(ctx, {"text": body, "mode": "summary"})
    assert out["text"].endswith("...")
    assert len(out["text"]) <= 2003


@pytest.mark.asyncio
async def test_summary_empty_llm_summary_falls_back(tmp_path: Path) -> None:
    llm = FakeLLMClient(text_replies=[json.dumps({"summary": "   "})])
    ctx = _ctx(tmp_path, llm)
    out = await handler(ctx, {"text": "raw", "mode": "summary"})
    assert out["used_llm"] is False
    assert out["text"] == "raw"


# --------------------------------------------------------------------------
# Definition + pure helpers
# --------------------------------------------------------------------------


def test_definition_shape() -> None:
    assert definition.ref == CAP_REF == "cap.email_parse"
    assert definition.secrets == ()
    assert "comms" in definition.tags
    # extra keys are forbidden by the input schema
    with pytest.raises(ValidationError):
        definition.input_schema(text="hi", bogus=1)


def test_input_schema_rejects_bad_mode() -> None:
    with pytest.raises(ValidationError):
        definition.input_schema(text="hi", mode="xml")


def test_naive_kv_first_key_wins_and_skips_non_kv_lines() -> None:
    text = "Foo: 1\nnot a kv line\nFoo: 2\nBar: hi there\n- Bullet: yes"
    out = _naive_kv(text)
    assert out == {"Foo": "1", "Bar": "hi there", "Bullet": "yes"}


def test_strip_code_fence_variants() -> None:
    assert _strip_code_fence("```json\n{}\n```") == "{}"
    assert _strip_code_fence("```\n[]\n```") == "[]"
    assert _strip_code_fence('{"a":1}') == '{"a":1}'


def test_loads_lenient_extracts_embedded_array() -> None:
    assert _loads_lenient('noise [1, 2, 3] more noise') == [1, 2, 3]
    with pytest.raises(ValueError):
        _loads_lenient("definitely not json")
