"""Tests for cap.data_validate.

Pure-stdlib capability — every path runs without optional dependencies. We
exercise the definition shape, schema compilation (including malformed-spec
rejection), each value-level check, cross-row uniqueness, the inline-vs-source
input modes, and the result partitioning.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from aakaar.capabilities.data.data_validate import (
    CAP_REF,
    compile_schema,
    definition,
    handler,
    validate_rows,
)
from aakaar.interpreter.activities.types import ActivityContext


def _ctx(tmp_path: Path) -> ActivityContext:
    from aakaar.shared.registry import build_default_registry
    from aakaar.storage import LocalFsObjectStore
    from aakaar.vault import LocalVault

    return ActivityContext(
        tenant_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        registry=build_default_registry(),
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=LocalVault(tmp_path / "vault"),
    )


def _errs(rows: list[dict[str, object]], schema: list[dict[str, object]]) -> list[dict[str, object]]:
    return [e.model_dump() for e in validate_rows(rows, compile_schema(schema))]


# --------------------------------------------------------------------------
# Definition + input validation
# --------------------------------------------------------------------------


def test_definition_shape() -> None:
    assert definition.ref == CAP_REF == "cap.data_validate"
    assert definition.secrets == ()
    assert "validate" in definition.tags


def test_definition_is_read_only() -> None:
    assert definition.side_effecting is False


def test_input_requires_exactly_one_source() -> None:
    schema = [{"field": "a"}]
    # neither rows nor source
    with pytest.raises(ValidationError):
        definition.input_schema(schema=schema)
    # both
    with pytest.raises(ValidationError):
        definition.input_schema(schema=schema, rows=[{"a": 1}], source="aakaar://t/x/y.json")
    # exactly one is fine
    definition.input_schema(schema=schema, rows=[{"a": 1}])


def test_schema_alias_accepts_schema_keyword() -> None:
    parsed = definition.input_schema(schema=[{"field": "a"}], rows=[])
    assert parsed.schema_ == [{"field": "a"}]


# --------------------------------------------------------------------------
# Schema compilation
# --------------------------------------------------------------------------


def test_compile_rejects_empty_schema() -> None:
    with pytest.raises(ValueError, match="non-empty list"):
        compile_schema([])


def test_compile_rejects_rule_without_field() -> None:
    with pytest.raises(ValueError, match="needs a `field`"):
        compile_schema([{"type": "string"}])


def test_compile_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="unsupported type"):
        compile_schema([{"field": "a", "type": "decimal"}])


def test_compile_rejects_invalid_regex() -> None:
    with pytest.raises(ValueError, match="invalid regex"):
        compile_schema([{"field": "a", "pattern": "([unclosed"}])


def test_compile_rejects_non_numeric_bound() -> None:
    with pytest.raises(ValueError, match="`min` must be a number"):
        compile_schema([{"field": "a", "min": "lots"}])


def test_compile_rejects_non_integer_length() -> None:
    with pytest.raises(ValueError, match="`min_length` must be an integer"):
        compile_schema([{"field": "a", "min_length": 1.5}])


def test_compile_rejects_negative_length() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        compile_schema([{"field": "a", "max_length": -1}])


# --------------------------------------------------------------------------
# Value-level checks
# --------------------------------------------------------------------------


def test_required_missing_and_empty() -> None:
    schema = [{"field": "id", "required": True}]
    errs = _errs([{"id": None}, {}, {"id": ""}, {"id": "ok"}], schema)
    bad_rows = {e["row"] for e in errs}
    assert bad_rows == {0, 1, 2}  # null, missing, empty all fail; row 3 passes


def test_optional_absent_is_ok() -> None:
    schema = [{"field": "memo", "required": False}]
    assert _errs([{}, {"memo": "x"}], schema) == []


def test_type_number_coerces_numeric_string() -> None:
    schema = [{"field": "amount", "type": "number"}]
    assert _errs([{"amount": "100.50"}, {"amount": 200}], schema) == []
    bad = _errs([{"amount": "abc"}], schema)
    assert bad and "expected a number" in bad[0]["error"]


def test_type_integer_rejects_fractional() -> None:
    schema = [{"field": "count", "type": "integer"}]
    assert _errs([{"count": "5"}, {"count": 5}], schema) == []
    bad = _errs([{"count": "5.5"}], schema)
    assert bad and "expected an integer" in bad[0]["error"]


def test_boolean_check() -> None:
    schema = [{"field": "flag", "type": "boolean"}]
    assert _errs([{"flag": True}, {"flag": "yes"}, {"flag": "0"}], schema) == []
    bad = _errs([{"flag": "maybe"}], schema)
    assert bad and "expected a boolean" in bad[0]["error"]


def test_allowed_values() -> None:
    schema = [{"field": "status", "allowed": ["OK", "FAIL"]}]
    assert _errs([{"status": "OK"}], schema) == []
    bad = _errs([{"status": "PENDING"}], schema)
    assert bad and "not in allowed set" in bad[0]["error"]


def test_numeric_bounds() -> None:
    schema = [{"field": "amount", "type": "number", "min": 0, "max": 1000}]
    assert _errs([{"amount": 500}], schema) == []
    errs = _errs([{"amount": -1}, {"amount": 2000}], schema)
    msgs = sorted(e["error"] for e in errs)
    assert any("below min" in m for m in msgs)
    assert any("above max" in m for m in msgs)


def test_length_and_pattern() -> None:
    schema = [
        {"field": "ifsc", "pattern": r"[A-Z]{4}0[A-Z0-9]{6}", "min_length": 11, "max_length": 11}
    ]
    assert _errs([{"ifsc": "HDFC0001234"}], schema) == []
    # A too-short value trips the length bound (checked before the pattern).
    short = _errs([{"ifsc": "bad"}], schema)
    assert short and "min_length" in short[0]["error"]
    # A correct-length value that violates the regex trips the pattern check.
    wrong = _errs([{"ifsc": "1234X001234"}], schema)
    assert wrong and "pattern" in wrong[0]["error"]


def test_unique_across_rows() -> None:
    schema = [{"field": "utr", "unique": True}]
    errs = _errs([{"utr": "A"}, {"utr": "B"}, {"utr": "A"}], schema)
    assert len(errs) == 1
    assert errs[0]["row"] == 2
    assert "not unique" in errs[0]["error"]
    assert "row 0" in errs[0]["error"]


def test_non_dict_row_is_flagged() -> None:
    schema = [{"field": "a"}]
    errs = _errs([["not", "a", "dict"]], schema)  # type: ignore[list-item]
    assert errs and errs[0]["field"] == "*"


# --------------------------------------------------------------------------
# Handler — inline rows + source, partitioning
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_inline_rows_partitions(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    schema = [
        {"field": "txn_id", "type": "integer", "unique": True},
        {"field": "amount", "type": "number", "min": 0},
        {"field": "status", "allowed": ["OK", "FAIL"]},
    ]
    rows = [
        {"txn_id": 1, "amount": 100, "status": "OK"},
        {"txn_id": 2, "amount": -5, "status": "OK"},  # bad amount
        {"txn_id": 1, "amount": 50, "status": "PENDING"},  # dup id + bad status
    ]
    out = await handler(ctx, {"schema": schema, "rows": rows})
    assert out["valid"] is False
    assert out["row_count"] == 3
    assert out["valid_count"] == 1
    assert out["invalid_count"] == 2
    assert out["valid_rows"] == [rows[0]]
    assert {e["row"] for e in out["errors"]} == {1, 2}


@pytest.mark.asyncio
async def test_handler_all_valid(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    schema = [{"field": "a", "type": "string"}]
    out = await handler(ctx, {"schema": schema, "rows": [{"a": "x"}, {"a": "y"}]})
    assert out["valid"] is True
    assert out["errors"] == []
    assert out["invalid_rows"] == []


@pytest.mark.asyncio
async def test_handler_source_json_array(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    payload = json.dumps([{"a": "1"}, {"a": "x"}]).encode()
    stored = ctx.object_store.put(str(ctx.tenant_id), "rows.json", payload)
    out = await handler(
        ctx, {"schema": [{"field": "a", "type": "integer"}], "source": stored.uri}
    )
    assert out["row_count"] == 2
    assert out["invalid_count"] == 1  # "x" is not an integer


@pytest.mark.asyncio
async def test_handler_source_ndjson(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    payload = b'{"a": 1}\n{"a": 2}\n'
    stored = ctx.object_store.put(str(ctx.tenant_id), "rows.ndjson", payload)
    out = await handler(ctx, {"schema": [{"field": "a"}], "source": stored.uri})
    assert out["row_count"] == 2
    assert out["valid"] is True


# --------------------------------------------------------------------------
# Safety property: row/source caps
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_rejects_too_many_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aakaar.capabilities.data.data_validate as mod

    monkeypatch.setattr(mod, "_MAX_ROWS", 2)
    ctx = _ctx(tmp_path)
    with pytest.raises(RuntimeError, match="exceeds the"):
        await handler(ctx, {"schema": [{"field": "a"}], "rows": [{"a": 1}] * 3})


@pytest.mark.asyncio
async def test_handler_rejects_oversized_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aakaar.capabilities.data.data_validate as mod

    monkeypatch.setattr(mod, "_MAX_SOURCE_BYTES", 4)
    ctx = _ctx(tmp_path)
    stored = ctx.object_store.put(str(ctx.tenant_id), "big.json", b"[]" + b" " * 64)
    with pytest.raises(RuntimeError, match="exceeding the"):
        await handler(ctx, {"schema": [{"field": "a"}], "source": stored.uri})
