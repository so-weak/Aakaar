"""Tests for cap.data_transform.

Drives the handler with a hand-built ActivityContext + a tmp object store
seeded with a small CSV, covering:
  - filter + sort + derive happy path (csv -> csv)
  - groupby aggregation with format conversion (csv -> json)
  - rename / dedupe / fillna
  - format inference + override, and json/xlsx round-trips
  - input validation + malformed-op errors
  - definition shape and pure helpers
"""

from __future__ import annotations

import io
import json
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

pd = pytest.importorskip("pandas")

from aakaar.capabilities.data.data_transform import (  # noqa: E402
    CAP_REF,
    _infer_format,
    apply_ops,
    definition,
    handler,
)
from aakaar.interpreter.activities.types import ActivityContext  # noqa: E402

_CSV = (
    "region,product,units,price\n"
    "west,apple,3,1.0\n"
    "east,apple,5,1.0\n"
    "west,pear,2,2.0\n"
    "east,pear,,2.0\n"
)


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


def _seed(ctx: ActivityContext, key: str, data: bytes) -> str:
    obj = ctx.object_store.put(str(ctx.tenant_id), key, data)
    return obj.uri


def _result_df(ctx: ActivityContext, uri: str, fmt: str):
    raw = ctx.object_store.get(uri)
    if fmt == "csv":
        return pd.read_csv(io.BytesIO(raw))
    if fmt == "json":
        return pd.read_json(io.StringIO(raw.decode("utf-8")))
    if fmt == "xlsx":
        return pd.read_excel(io.BytesIO(raw))
    raise AssertionError(fmt)


# --------------------------------------------------------------------------
# Handler happy paths
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_sort_derive_csv(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = _seed(ctx, "in/sales.csv", _CSV.encode("utf-8"))

    out = await handler(
        ctx,
        {
            "source": src,
            "ops": [
                {"op": "filter", "column": "product", "operator": "==", "value": "apple"},
                {"op": "derive", "column": "revenue", "expr": "units * price"},
                {"op": "sort", "by": "units", "ascending": False},
            ],
            "output_format": "csv",
        },
    )

    assert out["result_uri"].startswith("aakaar://t/")
    assert out["rows"] == 2
    assert out["columns"] == ["region", "product", "units", "price", "revenue"]

    df = _result_df(ctx, out["result_uri"], "csv")
    # sorted desc by units -> east(5) then west(3)
    assert list(df["region"]) == ["east", "west"]
    assert list(df["revenue"]) == [5.0, 3.0]


@pytest.mark.asyncio
async def test_groupby_to_json(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = _seed(ctx, "in/sales.csv", _CSV.encode("utf-8"))

    out = await handler(
        ctx,
        {
            "source": src,
            "ops": [
                {"op": "groupby", "by": "product", "agg": {"units": "sum"}},
                {"op": "sort", "by": "product"},
            ],
            "output_format": "json",
        },
    )

    assert out["rows"] == 2
    assert set(out["columns"]) == {"product", "units"}

    raw = ctx.object_store.get(out["result_uri"])
    records = json.loads(raw)
    by_product = {r["product"]: r["units"] for r in records}
    assert by_product["apple"] == 8  # 3 + 5
    assert by_product["pear"] == 2  # 2 + NaN(dropped by sum)


@pytest.mark.asyncio
async def test_fillna_then_aggregate(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = _seed(ctx, "in/sales.csv", _CSV.encode("utf-8"))

    out = await handler(
        ctx,
        {
            "source": src,
            "ops": [
                {"op": "fillna", "value": 0, "columns": "units"},
                {"op": "aggregate", "agg": {"units": "sum"}},
            ],
            "output_format": "csv",
        },
    )
    assert out["rows"] == 1
    df = _result_df(ctx, out["result_uri"], "csv")
    assert df["units"].iloc[0] == 10  # 3 + 5 + 2 + 0


@pytest.mark.asyncio
async def test_rename_and_dedupe(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = _seed(ctx, "in/sales.csv", _CSV.encode("utf-8"))

    out = await handler(
        ctx,
        {
            "source": src,
            "ops": [
                {"op": "rename", "columns": {"region": "zone"}},
                {"op": "dedupe", "subset": "zone", "keep": "first"},
            ],
            "output_format": "csv",
        },
    )
    assert "zone" in out["columns"]
    assert out["rows"] == 2  # west, east


@pytest.mark.asyncio
async def test_json_source_inferred_and_xlsx_roundtrip(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    records = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    src = _seed(ctx, "in/data.json", json.dumps(records).encode("utf-8"))

    out = await handler(
        ctx,
        {
            "source": src,
            "ops": [{"op": "filter", "column": "a", "operator": ">", "value": 1}],
            "output_format": "xlsx",
        },
    )
    assert out["rows"] == 1
    df = _result_df(ctx, out["result_uri"], "xlsx")
    assert list(df["b"]) == ["y"]


@pytest.mark.asyncio
async def test_source_format_override(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    # Key has no recognizable extension; rely on source_format override.
    src = _seed(ctx, "in/blob", _CSV.encode("utf-8"))
    out = await handler(
        ctx,
        {"source": src, "ops": [], "source_format": "csv", "output_format": "csv"},
    )
    assert out["rows"] == 4


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_op_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = _seed(ctx, "in/sales.csv", _CSV.encode("utf-8"))
    with pytest.raises(RuntimeError, match="unknown `op`"):
        await handler(ctx, {"source": src, "ops": [{"op": "explode"}]})


@pytest.mark.asyncio
async def test_filter_unknown_column_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = _seed(ctx, "in/sales.csv", _CSV.encode("utf-8"))
    with pytest.raises(RuntimeError, match="unknown column"):
        await handler(
            ctx,
            {
                "source": src,
                "ops": [{"op": "filter", "column": "nope", "operator": "==", "value": 1}],
            },
        )


@pytest.mark.asyncio
async def test_unknown_extension_without_override_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    src = _seed(ctx, "in/blob", _CSV.encode("utf-8"))
    with pytest.raises(RuntimeError, match="cannot infer format"):
        await handler(ctx, {"source": src, "ops": []})


# --------------------------------------------------------------------------
# Definition + pure helpers
# --------------------------------------------------------------------------


def test_definition_shape() -> None:
    assert definition.ref == CAP_REF == "cap.data_transform"
    assert definition.secrets == ()
    assert "data" in definition.tags
    with pytest.raises(ValidationError):
        definition.input_schema(source="aakaar://t/x/y.csv", bogus=1)


def test_input_schema_rejects_bad_output_format() -> None:
    with pytest.raises(ValidationError):
        definition.input_schema(source="aakaar://t/x/y.csv", output_format="parquet")


def test_infer_format() -> None:
    assert _infer_format("aakaar://t/x/a.csv", None) == "csv"
    assert _infer_format("aakaar://t/x/a.tsv", None) == "csv"
    assert _infer_format("aakaar://t/x/a.xlsx", None) == "xlsx"
    assert _infer_format("aakaar://t/x/a.json", None) == "json"
    assert _infer_format("aakaar://t/x/a.weird", "json") == "json"
    with pytest.raises(RuntimeError):
        _infer_format("aakaar://t/x/a.weird", None)


def test_apply_ops_groupby_pivot_pure() -> None:
    df = pd.DataFrame(
        {
            "region": ["w", "e", "w", "e"],
            "product": ["a", "a", "b", "b"],
            "units": [1, 2, 3, 4],
        }
    )
    grouped = apply_ops(df, [{"op": "groupby", "by": "region", "agg": {"units": "sum"}}])
    by_region = dict(zip(grouped["region"], grouped["units"], strict=True))
    assert by_region == {"w": 4, "e": 6}

    pivoted = apply_ops(
        df,
        [
            {
                "op": "pivot",
                "index": "region",
                "columns": "product",
                "values": "units",
                "aggfunc": "sum",
            }
        ],
    )
    assert "region" in pivoted.columns
    assert {"a", "b"}.issubset(set(pivoted.columns))


def test_apply_ops_rejects_bad_agg_func() -> None:
    df = pd.DataFrame({"x": [1, 2]})
    with pytest.raises(RuntimeError, match="unsupported"):
        apply_ops(df, [{"op": "aggregate", "agg": {"x": "__import__"}}])
