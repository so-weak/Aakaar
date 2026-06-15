"""cap.data_transform — apply a pipeline of tabular transforms with pandas.

Server-local, no-network, no-secrets capability. It reads a tabular file
the upstream graph already produced (CSV / XLSX / JSON) from object
storage, applies an ordered list of declarative `ops` with pandas, writes
the result back to object storage in the requested `output_format`, and
returns the new URI plus the result's row count and column names.

`source` is an `aakaar://` URI. The format is inferred from the key's
extension (`.csv`, `.tsv`, `.xlsx`/`.xls`, `.json`/`.jsonl`) unless
`source_format` overrides it.

`ops` is an ordered list of small dicts, each selected by its `op` key.
Every op is pure and deterministic (no I/O, no eval):

  - filter:    {op:"filter", column, operator, value}
      operator in {==, !=, >, >=, <, <=, in, not_in, contains,
      startswith, endswith, isnull, notnull}. `value` is ignored for
      isnull/notnull and must be a list for in/not_in.
  - sort:      {op:"sort", by: <col|[cols]>, ascending: <bool|[bool]>}
  - groupby:   {op:"groupby", by: <col|[cols]>, agg: {col: func, ...}}
      func in {sum, mean, min, max, count, median, std, first, last,
      nunique}. Reset to a flat frame.
  - aggregate: {op:"aggregate", agg: {col: func, ...}}
      whole-frame aggregation -> a single result row.
  - pivot:     {op:"pivot", index, columns, values, aggfunc: "mean"}
  - rename:    {op:"rename", columns: {old: new, ...}}
  - derive:    {op:"derive", column, expr}
      `expr` is evaluated with DataFrame.eval (column arithmetic only,
      no Python builtins / attribute access).
  - dedupe:    {op:"dedupe", subset: <col|[cols]|null>, keep: first|last|false}
  - fillna:    {op:"fillna", value, columns: <col|[cols]|null>}

Pandas is imported lazily inside the handler so module import never fails
when pandas is absent. The handler raises a clear RuntimeError if pandas
(or openpyxl, for .xlsx) is unavailable, or if an op is malformed.
"""

from __future__ import annotations

import io
import logging
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import CapabilityDefinition
from aakaar.storage.object_store import parse_uri

logger = logging.getLogger(__name__)
CAP_REF = "cap.data_transform"

# Pandas materializes the whole frame in memory (often at several times the
# on-disk size), so an unbounded source is a memory bomb; refuse early.
_MAX_SOURCE_BYTES = 64 * 1024 * 1024  # 64 MiB

_KNOWN_OPS = {
    "filter",
    "sort",
    "groupby",
    "pivot",
    "rename",
    "derive",
    "aggregate",
    "dedupe",
    "fillna",
}

_FILTER_OPERATORS = {
    "==",
    "!=",
    ">",
    ">=",
    "<",
    "<=",
    "in",
    "not_in",
    "contains",
    "startswith",
    "endswith",
    "isnull",
    "notnull",
}

# pandas-named aggregation funcs we allow by string. Keeping this an
# explicit allowlist avoids handing an arbitrary attribute name to pandas.
_AGG_FUNCS = {
    "sum",
    "mean",
    "min",
    "max",
    "count",
    "median",
    "std",
    "var",
    "first",
    "last",
    "nunique",
    "prod",
}

_EXT_FORMATS = {
    ".csv": "csv",
    ".tsv": "csv",
    ".xlsx": "xlsx",
    ".xls": "xlsx",
    ".json": "json",
    ".jsonl": "json",
}


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(
        description="aakaar:// URI of the input table (csv/xlsx/json) to transform.",
    )
    ops: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Ordered list of transform ops. Each is a dict keyed by `op` in "
            "{filter, sort, groupby, pivot, rename, derive, aggregate, dedupe, "
            "fillna}. See the capability docstring for each op's fields."
        ),
    )
    source_format: Literal["csv", "xlsx", "json"] | None = Field(
        default=None,
        description=(
            "Override the input format. By default it is inferred from the "
            "source URI's file extension."
        ),
    )
    output_format: Literal["csv", "xlsx", "json"] = Field(
        default="csv",
        description="Format to write the result in.",
    )


class _Outputs(BaseModel):
    result_uri: str = Field(description="aakaar:// URI of the transformed table.")
    rows: int = Field(description="Number of rows in the result.")
    columns: list[str] = Field(description="Column names in the result, in order.")


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Apply an ordered pipeline of declarative pandas transforms (filter, "
        "sort, groupby, pivot, rename, derive, aggregate, dedupe, fillna) to a "
        "CSV/XLSX/JSON file in object storage and write the result back. "
        "Server-local, no network, no credentials."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("data", "transform", "pandas", "tabular"),
)


# --------------------------------------------------------------------------
# Pure helpers (no ctx / no I/O) — unit-testable in isolation.
# --------------------------------------------------------------------------


def _infer_format(uri: str, override: str | None) -> str:
    if override:
        return override
    _, key = parse_uri(uri)
    lower = key.lower()
    for ext, fmt in _EXT_FORMATS.items():
        if lower.endswith(ext):
            return fmt
    raise RuntimeError(
        f"cap.data_transform: cannot infer format from {uri!r}; set "
        f"`source_format` explicitly (csv/xlsx/json)"
    )


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _ext_for_format(fmt: str) -> str:
    return {"csv": "csv", "xlsx": "xlsx", "json": "json"}[fmt]


def _read_frame(data: bytes, fmt: str, *, sep_tsv: bool) -> Any:
    import pandas as pd

    if fmt == "csv":
        sep = "\t" if sep_tsv else ","
        return pd.read_csv(io.BytesIO(data), sep=sep)
    if fmt == "xlsx":
        try:
            return pd.read_excel(io.BytesIO(data))
        except ImportError as e:  # pragma: no cover - openpyxl is installed
            raise RuntimeError(
                "cap.data_transform: reading xlsx requires the 'openpyxl' "
                f"package, which is not available: {e}"
            ) from e
    if fmt == "json":
        text = data.decode("utf-8")
        # Support both a JSON array/object of records and newline-delimited
        # JSON. Try the standard reader first; fall back to JSON Lines.
        try:
            return pd.read_json(io.StringIO(text))
        except ValueError:
            return pd.read_json(io.StringIO(text), lines=True)
    raise RuntimeError(f"cap.data_transform: unsupported source_format {fmt!r}")


def _write_frame(df: Any, fmt: str) -> bytes:
    if fmt == "csv":
        csv_text: str = df.to_csv(index=False)
        return csv_text.encode("utf-8")
    if fmt == "json":
        # records orientation -> list[ {col: val} ], the friendliest shape
        # for downstream nodes / cap.email_parse style consumers.
        json_text: str = df.to_json(orient="records")
        return json_text.encode("utf-8")
    if fmt == "xlsx":
        buf = io.BytesIO()
        try:
            with __import__("pandas").ExcelWriter(buf, engine="openpyxl") as writer:
                df.to_excel(writer, index=False)
        except ImportError as e:  # pragma: no cover - openpyxl is installed
            raise RuntimeError(
                "cap.data_transform: writing xlsx requires the 'openpyxl' "
                f"package, which is not available: {e}"
            ) from e
        return buf.getvalue()
    raise RuntimeError(f"cap.data_transform: unsupported output_format {fmt!r}")


def _require_columns(df: Any, columns: list[str], op: str) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise RuntimeError(
            f"cap.data_transform: op {op!r} references unknown column(s) "
            f"{missing!r}; available: {list(df.columns)!r}"
        )


def _check_agg_funcs(agg: dict[str, Any], op: str) -> None:
    for col, func in agg.items():
        if not isinstance(func, str) or func not in _AGG_FUNCS:
            raise RuntimeError(
                f"cap.data_transform: op {op!r} column {col!r} has unsupported "
                f"agg func {func!r}; allowed: {sorted(_AGG_FUNCS)}"
            )


def _apply_filter(df: Any, spec: dict[str, Any]) -> Any:
    column = spec.get("column")
    operator = spec.get("operator")
    if not isinstance(column, str):
        raise RuntimeError("cap.data_transform: filter requires a `column` string")
    if operator not in _FILTER_OPERATORS:
        raise RuntimeError(
            f"cap.data_transform: filter has unsupported operator {operator!r}; "
            f"allowed: {sorted(_FILTER_OPERATORS)}"
        )
    _require_columns(df, [column], "filter")
    series = df[column]
    value = spec.get("value")

    if operator == "isnull":
        return df[series.isnull()]
    if operator == "notnull":
        return df[series.notnull()]
    if operator == "==":
        return df[series == value]
    if operator == "!=":
        return df[series != value]
    if operator == ">":
        return df[series > value]
    if operator == ">=":
        return df[series >= value]
    if operator == "<":
        return df[series < value]
    if operator == "<=":
        return df[series <= value]
    if operator in ("in", "not_in"):
        if not isinstance(value, (list, tuple)):
            raise RuntimeError(
                f"cap.data_transform: filter operator {operator!r} needs a list "
                f"`value`, got {type(value).__name__}"
            )
        mask = series.isin(list(value))
        return df[mask] if operator == "in" else df[~mask]
    # string ops
    str_series = series.astype("string")
    needle = "" if value is None else str(value)
    if operator == "contains":
        return df[str_series.str.contains(needle, na=False, regex=False)]
    if operator == "startswith":
        return df[str_series.str.startswith(needle, na=False)]
    if operator == "endswith":
        return df[str_series.str.endswith(needle, na=False)]
    raise RuntimeError(f"cap.data_transform: unhandled filter operator {operator!r}")


def _apply_sort(df: Any, spec: dict[str, Any]) -> Any:
    by = _as_list(spec.get("by"))
    if not by:
        raise RuntimeError("cap.data_transform: sort requires `by`")
    _require_columns(df, [str(c) for c in by], "sort")
    ascending = spec.get("ascending", True)
    return df.sort_values(by=by, ascending=ascending, kind="stable").reset_index(
        drop=True
    )


def _apply_groupby(df: Any, spec: dict[str, Any]) -> Any:
    by = _as_list(spec.get("by"))
    agg = spec.get("agg")
    if not by:
        raise RuntimeError("cap.data_transform: groupby requires `by`")
    if not isinstance(agg, dict) or not agg:
        raise RuntimeError("cap.data_transform: groupby requires a non-empty `agg` map")
    _require_columns(df, [str(c) for c in by], "groupby")
    _require_columns(df, list(agg.keys()), "groupby")
    _check_agg_funcs(agg, "groupby")
    grouped = df.groupby(by, dropna=False, as_index=False).agg(agg)
    return grouped.reset_index(drop=True)


def _apply_aggregate(df: Any, spec: dict[str, Any]) -> Any:
    import pandas as pd

    agg = spec.get("agg")
    if not isinstance(agg, dict) or not agg:
        raise RuntimeError(
            "cap.data_transform: aggregate requires a non-empty `agg` map"
        )
    _require_columns(df, list(agg.keys()), "aggregate")
    _check_agg_funcs(agg, "aggregate")
    result: dict[str, Any] = {}
    for col, func in agg.items():
        result[col] = getattr(df[col], func)()
    return pd.DataFrame([result])


def _apply_pivot(df: Any, spec: dict[str, Any]) -> Any:
    index = spec.get("index")
    columns = spec.get("columns")
    values = spec.get("values")
    aggfunc = spec.get("aggfunc", "mean")
    if not (index and columns and values):
        raise RuntimeError(
            "cap.data_transform: pivot requires `index`, `columns` and `values`"
        )
    if aggfunc not in _AGG_FUNCS:
        raise RuntimeError(
            f"cap.data_transform: pivot has unsupported aggfunc {aggfunc!r}; "
            f"allowed: {sorted(_AGG_FUNCS)}"
        )
    cols_used = _as_list(index) + _as_list(columns) + _as_list(values)
    _require_columns(df, [str(c) for c in cols_used], "pivot")
    pivoted = df.pivot_table(
        index=index, columns=columns, values=values, aggfunc=aggfunc
    )
    # Flatten any MultiIndex columns into plain strings and lift the index
    # back into a column so the result round-trips cleanly to csv/json.
    flat = pivoted.reset_index()
    flat.columns = [
        "_".join(str(p) for p in c if str(p) != "") if isinstance(c, tuple) else str(c)
        for c in flat.columns
    ]
    return flat


def _apply_rename(df: Any, spec: dict[str, Any]) -> Any:
    mapping = spec.get("columns")
    if not isinstance(mapping, dict) or not mapping:
        raise RuntimeError(
            "cap.data_transform: rename requires a non-empty `columns` map"
        )
    _require_columns(df, list(mapping.keys()), "rename")
    return df.rename(columns=mapping)


def _apply_derive(df: Any, spec: dict[str, Any]) -> Any:
    column = spec.get("column")
    expr = spec.get("expr")
    if not isinstance(column, str) or not column:
        raise RuntimeError("cap.data_transform: derive requires a `column` string")
    if not isinstance(expr, str) or not expr:
        raise RuntimeError("cap.data_transform: derive requires an `expr` string")
    out = df.copy()
    try:
        # engine="python" is required for general expressions; eval only sees
        # the frame's columns plus literals — no Python builtins, no attribute
        # access, no statements.
        out[column] = df.eval(expr, engine="python")
    except Exception as e:
        raise RuntimeError(
            f"cap.data_transform: derive expr {expr!r} failed: {e}"
        ) from e
    return out


def _apply_dedupe(df: Any, spec: dict[str, Any]) -> Any:
    subset_raw = spec.get("subset")
    subset = _as_list(subset_raw) if subset_raw is not None else None
    if subset:
        _require_columns(df, [str(c) for c in subset], "dedupe")
    keep = spec.get("keep", "first")
    if keep not in ("first", "last", False):
        raise RuntimeError(
            f"cap.data_transform: dedupe `keep` must be 'first', 'last' or false, "
            f"got {keep!r}"
        )
    return df.drop_duplicates(subset=subset, keep=keep).reset_index(drop=True)


def _apply_fillna(df: Any, spec: dict[str, Any]) -> Any:
    if "value" not in spec:
        raise RuntimeError("cap.data_transform: fillna requires a `value`")
    value = spec["value"]
    cols_raw = spec.get("columns")
    if cols_raw is None:
        return df.fillna(value)
    cols = _as_list(cols_raw)
    _require_columns(df, [str(c) for c in cols], "fillna")
    out = df.copy()
    out[cols] = out[cols].fillna(value)
    return out


_OP_DISPATCH = {
    "filter": _apply_filter,
    "sort": _apply_sort,
    "groupby": _apply_groupby,
    "aggregate": _apply_aggregate,
    "pivot": _apply_pivot,
    "rename": _apply_rename,
    "derive": _apply_derive,
    "dedupe": _apply_dedupe,
    "fillna": _apply_fillna,
}


def apply_ops(df: Any, ops: list[dict[str, Any]]) -> Any:
    """Apply the op pipeline to a DataFrame, returning the result frame.

    Pure: no ctx, no I/O. Raises RuntimeError on a malformed op."""
    for idx, spec in enumerate(ops):
        if not isinstance(spec, dict):
            raise RuntimeError(
                f"cap.data_transform: op #{idx} is not an object: {spec!r}"
            )
        name = spec.get("op")
        if name not in _KNOWN_OPS:
            raise RuntimeError(
                f"cap.data_transform: op #{idx} has unknown `op` {name!r}; "
                f"supported: {sorted(_KNOWN_OPS)}"
            )
        df = _OP_DISPATCH[name](df, spec)
        logger.debug("cap.data_transform applied op #%d %s", idx, name)
    return df


# --------------------------------------------------------------------------
# Handler
# --------------------------------------------------------------------------


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    try:
        import pandas as pd  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "cap.data_transform requires the 'pandas' package, which is not "
            f"available in this environment: {e}"
        ) from e

    source = inputs["source"]
    ops = inputs.get("ops") or []
    output_format = inputs.get("output_format", "csv")
    source_format = _infer_format(source, inputs.get("source_format"))

    _, src_key = parse_uri(source)
    is_tsv = src_key.lower().endswith(".tsv")

    logger.info(
        "cap.data_transform start run_id=%s source=%s in_fmt=%s out_fmt=%s ops=%d",
        ctx.run_id,
        source,
        source_format,
        output_format,
        len(ops),
    )

    raw = ctx.object_store.get(source)
    if len(raw) > _MAX_SOURCE_BYTES:
        raise RuntimeError(
            f"cap.data_transform: source is {len(raw)} bytes, exceeding the "
            f"{_MAX_SOURCE_BYTES}-byte limit"
        )
    df = _read_frame(raw, source_format, sep_tsv=is_tsv)
    df = apply_ops(df, ops)

    out_bytes = _write_frame(df, output_format)

    ext = _ext_for_format(output_format)
    key = f"runs/{ctx.run_id}/transforms/{uuid.uuid4().hex}.{ext}"
    obj = ctx.object_store.put(str(ctx.tenant_id), key, out_bytes)

    columns = [str(c) for c in df.columns]
    rows = int(len(df))
    logger.info(
        "cap.data_transform ok run_id=%s result_uri=%s rows=%d cols=%d bytes=%d",
        ctx.run_id,
        obj.uri,
        rows,
        len(columns),
        len(out_bytes),
    )
    return {"result_uri": obj.uri, "rows": rows, "columns": columns}
