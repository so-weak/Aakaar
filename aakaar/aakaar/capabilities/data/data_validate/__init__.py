"""cap.data_validate — validate tabular records against a simple field schema.

Pure-stdlib, server-local, read-only. Built for banking reconciliation: after
a settlement / statement file is read into rows (e.g. by
``cap.spreadsheet_read`` or ``cap.doc_extract``), validate every record against
a declarative ``schema`` before reconciling, so malformed or out-of-policy rows
are caught with a precise, per-row, per-field reason instead of failing deep in
a downstream join.

Rows come from either ``rows`` (inline, the common case when chaining after a
read capability) or ``source`` (an ``aakaar://`` URI of a JSON array / NDJSON of
objects) — exactly one must be supplied.

``schema`` is a list of field rules. Each rule names a ``field`` and may set:

  - type:      one of "string", "number", "integer", "boolean", "any".
               Strings that look numeric are accepted for number/integer when
               ``coerce`` is true (default) — file readers often yield "100.50".
  - required:  bool (default true). A required field that is missing or null
               (or empty-string when ``allow_empty`` is false) fails.
  - allow_empty: bool (default false). Whether "" counts as present.
  - allowed:   list of permitted values (membership check after coercion).
  - min / max: numeric bounds (inclusive); only meaningful for number/integer.
  - min_length / max_length: string length bounds.
  - pattern:   a regex the (stringified) value must fully match. Compiled once;
               an invalid pattern is a schema error, surfaced clearly.
  - unique:    bool. The field's values must be unique across all rows; the
               second+ occurrence fails with an "is not unique" error.

The result reports ``valid`` (no errors anywhere), the per-row ``errors``, the
``valid_rows`` / ``invalid_rows`` partitions, and counts. Nothing is written and
no network is touched (``side_effecting=False``); on a dry-run it executes for
real so a simulated plan still surfaces real validation findings.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import CapabilityDefinition

logger = logging.getLogger(__name__)
CAP_REF = "cap.data_validate"

# Validation is O(rows x fields) in memory; bound both so a hand-written DAG
# can't turn this into a CPU/memory bomb.
_MAX_ROWS = 1_000_000
_MAX_FIELDS = 1_024
_MAX_SOURCE_BYTES = 32 * 1024 * 1024  # 32 MiB when reading from a URI

_TYPES = {"string", "number", "integer", "boolean", "any"}


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_: list[dict[str, Any]] = Field(
        alias="schema",
        description=(
            "List of field rules. Each rule: {field, type?, required?, "
            "allow_empty?, allowed?, min?, max?, min_length?, max_length?, "
            "pattern?, unique?, coerce?}. See the capability docstring."
        ),
    )
    rows: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Inline records to validate. Mutually exclusive with `source`. "
            "Use this when chaining after a read capability."
        ),
    )
    source: str | None = Field(
        default=None,
        description=(
            "aakaar:// URI of a JSON array (or NDJSON) of record objects. "
            "Mutually exclusive with `rows`."
        ),
    )

    @model_validator(mode="after")
    def _check_one_source(self) -> _Inputs:
        provided = sum(1 for v in (self.rows, self.source) if v is not None)
        if provided != 1:
            raise ValueError("exactly one of `rows` or `source` must be provided")
        return self


class _RowError(BaseModel):
    row: int = Field(description="0-based index of the offending row.")
    field: str = Field(description="Field the error is about.")
    error: str = Field(description="Human-readable reason the value is invalid.")


class _Outputs(BaseModel):
    valid: bool = Field(description="True when no row violated any rule.")
    row_count: int = Field(description="Total rows validated.")
    valid_count: int = Field(description="Number of rows with no errors.")
    invalid_count: int = Field(description="Number of rows with at least one error.")
    errors: list[_RowError] = Field(
        description="Flat list of every (row, field, error) violation found.",
    )
    valid_rows: list[dict[str, Any]] = Field(
        description="The subset of input rows that passed every rule.",
    )
    invalid_rows: list[dict[str, Any]] = Field(
        description="The subset of input rows that failed at least one rule.",
    )


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Validate tabular records against a simple declarative field schema "
        "(type, required, allowed values, numeric/length bounds, regex, "
        "cross-row uniqueness). Returns per-row errors and valid/invalid "
        "partitions — built for catching malformed rows before a banking "
        "reconciliation. Pure stdlib, read-only, no secrets, no network."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    # Pure compute over already-fetched rows: nothing escapes the run sandbox.
    side_effecting=False,
    secrets=(),
    tags=("data", "validate", "schema", "quality", "recon"),
)


# ---------------------------------------------------------------------------
# Schema compilation (pure) — fail fast on a malformed schema spec.
# ---------------------------------------------------------------------------


def _numeric_bound(value: Any, field: str, name: str) -> float | None:
    """Validate a numeric bound from the schema spec, or None when absent."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"data_validate: field {field!r} `{name}` must be a number, got {value!r}"
        )
    return float(value)


def _int_bound(value: Any, field: str, name: str) -> int | None:
    """Validate an integer length bound from the schema spec, or None when absent."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"data_validate: field {field!r} `{name}` must be an integer, got {value!r}"
        )
    if value < 0:
        raise ValueError(
            f"data_validate: field {field!r} `{name}` must be non-negative, got {value!r}"
        )
    return value


class _FieldRule:
    """A compiled, validated single-field rule. Construction raises ValueError
    on a malformed spec so the error points at the schema, not the data."""

    __slots__ = (
        "field",
        "type",
        "required",
        "allow_empty",
        "allowed",
        "min",
        "max",
        "min_length",
        "max_length",
        "pattern",
        "unique",
        "coerce",
    )

    def __init__(self, spec: dict[str, Any]) -> None:
        field = spec.get("field")
        if not isinstance(field, str) or not field:
            raise ValueError(f"data_validate: each schema rule needs a `field` string: {spec!r}")
        self.field = field
        ftype = spec.get("type", "any")
        if ftype not in _TYPES:
            raise ValueError(
                f"data_validate: field {field!r} has unsupported type {ftype!r}; "
                f"allowed: {sorted(_TYPES)}"
            )
        self.type = ftype
        self.required = bool(spec.get("required", True))
        self.allow_empty = bool(spec.get("allow_empty", False))
        self.coerce = bool(spec.get("coerce", True))
        allowed = spec.get("allowed")
        if allowed is not None and not isinstance(allowed, (list, tuple)):
            raise ValueError(f"data_validate: field {field!r} `allowed` must be a list")
        self.allowed = list(allowed) if allowed is not None else None
        # Numeric bounds must themselves be numeric — catch a typo here (a
        # schema-author mistake) rather than crashing later on a per-row cast.
        self.min = _numeric_bound(spec.get("min"), field, "min")
        self.max = _numeric_bound(spec.get("max"), field, "max")
        self.min_length = _int_bound(spec.get("min_length"), field, "min_length")
        self.max_length = _int_bound(spec.get("max_length"), field, "max_length")
        self.unique = bool(spec.get("unique", False))
        pat = spec.get("pattern")
        if pat is not None:
            if not isinstance(pat, str):
                raise ValueError(f"data_validate: field {field!r} `pattern` must be a string")
            try:
                self.pattern: re.Pattern[str] | None = re.compile(pat)
            except re.error as e:
                raise ValueError(
                    f"data_validate: field {field!r} has an invalid regex pattern {pat!r}: {e}"
                ) from e
        else:
            self.pattern = None


def compile_schema(schema: list[dict[str, Any]]) -> list[_FieldRule]:
    if not isinstance(schema, list) or not schema:
        raise ValueError("data_validate: `schema` must be a non-empty list of field rules")
    if len(schema) > _MAX_FIELDS:
        raise ValueError(
            f"data_validate: schema has {len(schema)} rules, exceeding the "
            f"{_MAX_FIELDS}-field limit"
        )
    rules = [_FieldRule(spec) for spec in schema]
    return rules


# ---------------------------------------------------------------------------
# Value-level checks (pure) — return an error string, or None when the value
# satisfies the rule.
# ---------------------------------------------------------------------------


def _as_number(value: Any, coerce: bool) -> float | None:
    if isinstance(value, bool):  # bool is an int subclass; treat as non-numeric here
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if coerce and isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _check_type(rule: _FieldRule, value: Any) -> str | None:
    t = rule.type
    if t == "any":
        return None
    if t == "string":
        return None if isinstance(value, str) else f"expected a string, got {type(value).__name__}"
    if t == "boolean":
        if isinstance(value, bool):
            return None
        if rule.coerce and isinstance(value, str) and value.strip().lower() in {
            "true",
            "false",
            "1",
            "0",
            "yes",
            "no",
        }:
            return None
        return f"expected a boolean, got {type(value).__name__}"
    if t in ("number", "integer"):
        num = _as_number(value, rule.coerce)
        if num is None:
            return f"expected a {t}, got {value!r}"
        if t == "integer" and not float(num).is_integer():
            return f"expected an integer, got {value!r}"
        return None
    return None


def _check_bounds_and_pattern(rule: _FieldRule, value: Any) -> str | None:
    # Numeric bounds (only when a numeric value is available).
    if rule.min is not None or rule.max is not None:
        num = _as_number(value, rule.coerce)
        if num is not None:
            if rule.min is not None and num < float(rule.min):
                return f"value {num} is below min {rule.min}"
            if rule.max is not None and num > float(rule.max):
                return f"value {num} is above max {rule.max}"
    # String length + regex (operate on the stringified value).
    if (
        rule.min_length is not None
        or rule.max_length is not None
        or rule.pattern is not None
    ):
        s = value if isinstance(value, str) else str(value)
        if rule.min_length is not None and len(s) < int(rule.min_length):
            return f"length {len(s)} is below min_length {rule.min_length}"
        if rule.max_length is not None and len(s) > int(rule.max_length):
            return f"length {len(s)} is above max_length {rule.max_length}"
        if rule.pattern is not None and rule.pattern.fullmatch(s) is None:
            return f"value {s!r} does not match pattern {rule.pattern.pattern!r}"
    return None


def _check_allowed(rule: _FieldRule, value: Any) -> str | None:
    if rule.allowed is None:
        return None
    if value in rule.allowed:
        return None
    # Be lenient about scalar string/number mismatches from file readers.
    if rule.coerce:
        sval = str(value)
        if any(str(a) == sval for a in rule.allowed):
            return None
    return f"value {value!r} is not in allowed set {rule.allowed!r}"


def _is_present(value: Any, allow_empty: bool) -> bool:
    if value is None:
        return False
    # An empty/whitespace string counts as absent unless allow_empty is set.
    return allow_empty or not (isinstance(value, str) and value.strip() == "")


def validate_rows(
    rows: list[dict[str, Any]], rules: list[_FieldRule]
) -> list[_RowError]:
    """Validate every row against the compiled rules. Pure: no ctx, no I/O.

    Returns a flat list of (row, field, error) violations, in row-then-rule
    order. Cross-row ``unique`` rules are tracked across the whole pass.
    """
    errors: list[_RowError] = []
    # field -> first row index that used each seen value (for unique checks).
    seen_values: dict[str, dict[Any, int]] = {
        r.field: {} for r in rules if r.unique
    }

    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(_RowError(row=idx, field="*", error=f"row is not an object: {row!r}"))
            continue
        for rule in rules:
            present = rule.field in row and _is_present(row.get(rule.field), rule.allow_empty)
            if not present:
                if rule.required:
                    errors.append(
                        _RowError(row=idx, field=rule.field, error="required value is missing or empty")
                    )
                # Optional + absent: nothing else to check for this field.
                continue
            value = row[rule.field]

            type_err = _check_type(rule, value)
            if type_err is not None:
                errors.append(_RowError(row=idx, field=rule.field, error=type_err))
                # A wrong-typed value makes bound/pattern messages noisy; stop here.
                continue

            for check in (_check_allowed, _check_bounds_and_pattern):
                msg = check(rule, value)
                if msg is not None:
                    errors.append(_RowError(row=idx, field=rule.field, error=msg))

            if rule.unique:
                bucket = seen_values[rule.field]
                # Hash on the stringified value so unhashable/odd types don't crash.
                key = value if isinstance(value, (str, int, float, bool)) else str(value)
                if key in bucket:
                    errors.append(
                        _RowError(
                            row=idx,
                            field=rule.field,
                            error=f"value {value!r} is not unique (first seen at row {bucket[key]})",
                        )
                    )
                else:
                    bucket[key] = idx
    return errors


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def _load_source_rows(raw: bytes) -> list[dict[str, Any]]:
    """Parse a stored JSON array or NDJSON of objects into row dicts."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # Fall back to NDJSON: one JSON object per non-blank line.
        rows: list[dict[str, Any]] = []
        for ln, line in enumerate(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError) as e:
                raise RuntimeError(
                    f"cap.data_validate: source line {ln + 1} is not valid JSON: {e}"
                ) from e
            rows.append(obj)
        return rows
    if isinstance(value, list):
        return list(value)
    raise RuntimeError(
        "cap.data_validate: source JSON must be an array of objects (or NDJSON)"
    )


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    rules = compile_schema(inputs["schema"])

    rows = inputs.get("rows")
    if rows is None:
        source = inputs["source"]
        raw = ctx.object_store.get(source)
        if len(raw) > _MAX_SOURCE_BYTES:
            raise RuntimeError(
                f"cap.data_validate: source is {len(raw)} bytes, exceeding the "
                f"{_MAX_SOURCE_BYTES}-byte limit"
            )
        rows = _load_source_rows(raw)

    if not isinstance(rows, list):
        raise RuntimeError("cap.data_validate: `rows` must be a list of objects")
    if len(rows) > _MAX_ROWS:
        raise RuntimeError(
            f"cap.data_validate: {len(rows)} rows exceeds the {_MAX_ROWS}-row limit"
        )

    logger.info(
        "cap.data_validate start run_id=%s rows=%d rules=%d",
        ctx.run_id,
        len(rows),
        len(rules),
    )

    errors = validate_rows(rows, rules)
    bad_indices = {e.row for e in errors}
    valid_rows = [r for i, r in enumerate(rows) if i not in bad_indices]
    invalid_rows = [r for i, r in enumerate(rows) if i in bad_indices]

    logger.info(
        "cap.data_validate ok run_id=%s rows=%d valid=%d invalid=%d errors=%d",
        ctx.run_id,
        len(rows),
        len(valid_rows),
        len(invalid_rows),
        len(errors),
    )
    return {
        "valid": not errors,
        "row_count": len(rows),
        "valid_count": len(valid_rows),
        "invalid_count": len(invalid_rows),
        "errors": [e.model_dump() for e in errors],
        "valid_rows": valid_rows,
        "invalid_rows": invalid_rows,
    }
