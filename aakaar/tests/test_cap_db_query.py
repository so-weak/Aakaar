"""Tests for cap.db_query.

Real happy-path coverage for the sqlite engine (local file + managed-storage
URI), plus the pure DSN/param helpers and input/definition validation.
postgres/mysql need a live server (and psycopg/pymysql, which aren't
installed) so those branches are exercised only for their "missing driver"
RuntimeError where possible and otherwise skipped.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from aakaar.capabilities.data.db_query import (
    CAP_REF,
    _normalize_params,
    _parse_net_dsn,
    _rows_to_dicts,
    definition,
    handler,
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


def _make_sqlite_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, qty INTEGER)")
        conn.executemany(
            "INSERT INTO items (id, name, qty) VALUES (?, ?, ?)",
            [(1, "apple", 3), (2, "banana", 7), (3, "cherry", 2)],
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# sqlite happy paths
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_local_file_select(tmp_path: Path) -> None:
    db = tmp_path / "data.sqlite"
    _make_sqlite_db(db)
    ctx = _ctx(tmp_path)

    out = await handler(
        ctx,
        {"engine": "sqlite", "dsn": str(db), "sql": "SELECT id, name, qty FROM items ORDER BY id"},
    )

    assert out["columns"] == ["id", "name", "qty"]
    assert out["rowcount"] == 3
    assert out["rows"][0] == {"id": 1, "name": "apple", "qty": 3}
    assert out["rows"][-1] == {"id": 3, "name": "cherry", "qty": 2}


@pytest.mark.asyncio
async def test_sqlite_bound_params(tmp_path: Path) -> None:
    db = tmp_path / "data.sqlite"
    _make_sqlite_db(db)
    ctx = _ctx(tmp_path)

    out = await handler(
        ctx,
        {
            "engine": "sqlite",
            "dsn": str(db),
            "sql": "SELECT name FROM items WHERE qty > ? ORDER BY name",
            "params": [2],
        },
    )

    assert out["columns"] == ["name"]
    assert [r["name"] for r in out["rows"]] == ["apple", "banana"]
    assert out["rowcount"] == 2


@pytest.mark.asyncio
async def test_sqlite_max_rows_caps_results(tmp_path: Path) -> None:
    db = tmp_path / "data.sqlite"
    _make_sqlite_db(db)
    ctx = _ctx(tmp_path)

    out = await handler(
        ctx,
        {"engine": "sqlite", "dsn": str(db), "sql": "SELECT id FROM items ORDER BY id", "max_rows": 2},
    )

    assert out["rowcount"] == 2
    assert [r["id"] for r in out["rows"]] == [1, 2]


@pytest.mark.asyncio
async def test_sqlite_default_engine_is_sqlite(tmp_path: Path) -> None:
    db = tmp_path / "data.sqlite"
    _make_sqlite_db(db)
    ctx = _ctx(tmp_path)

    # engine omitted -> defaults to sqlite via the schema.
    validated = definition.input_schema(dsn=str(db), sql="SELECT COUNT(*) AS n FROM items")
    out = await handler(ctx, validated.model_dump())

    assert out["columns"] == ["n"]
    assert out["rows"] == [{"n": 3}]


@pytest.mark.asyncio
async def test_sqlite_from_object_store_uri(tmp_path: Path) -> None:
    db = tmp_path / "src.sqlite"
    _make_sqlite_db(db)
    ctx = _ctx(tmp_path)
    stored = ctx.object_store.put_file(str(ctx.tenant_id), "dbs/data.sqlite", db)

    out = await handler(
        ctx,
        {"engine": "sqlite", "dsn": stored.uri, "sql": "SELECT name FROM items WHERE id = ?", "params": [2]},
    )

    assert out["rows"] == [{"name": "banana"}]
    assert out["rowcount"] == 1


@pytest.mark.asyncio
async def test_sqlite_read_only_rejects_writes(tmp_path: Path) -> None:
    db = tmp_path / "data.sqlite"
    _make_sqlite_db(db)
    ctx = _ctx(tmp_path)

    with pytest.raises(sqlite3.OperationalError):
        await handler(
            ctx,
            {"engine": "sqlite", "dsn": str(db), "sql": "INSERT INTO items (id, name, qty) VALUES (9, 'z', 0)"},
        )


# --------------------------------------------------------------------------
# Validation + missing-driver behavior
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_sql_rejected(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(ValueError, match="non-empty string"):
        await handler(ctx, {"engine": "sqlite", "dsn": str(tmp_path / "x.sqlite"), "sql": "   "})


@pytest.mark.asyncio
async def test_network_engine_requires_alias(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(PermissionError, match="account_alias"):
        await handler(ctx, {"engine": "postgres", "dsn": "db.local/app", "sql": "SELECT 1"})


@pytest.mark.asyncio
async def test_postgres_missing_driver_is_clear(tmp_path: Path) -> None:
    pytest.importorskip  # noqa: B018 - keep import-time clean
    try:
        import psycopg  # type: ignore  # noqa: F401
        pytest.skip("psycopg is installed; missing-driver branch not exercised")
    except ImportError:
        pass

    ctx = _ctx(tmp_path)
    vault_ref = f"grants/{uuid.uuid4()}"
    ctx.vault.put(str(ctx.tenant_id), vault_ref, {"db_password": "secret"})
    ctx.granted_capabilities = {
        CAP_REF: {"primary": {"vault_ref": vault_ref, "input_defaults": {}}}
    }

    with pytest.raises(RuntimeError, match="psycopg"):
        await handler(
            ctx,
            {
                "engine": "postgres",
                "dsn": "db.local:5432/app?user=svc",
                "sql": "SELECT 1",
                "account_alias": "primary",
            },
        )


@pytest.mark.asyncio
async def test_mysql_missing_driver_is_clear(tmp_path: Path) -> None:
    try:
        import pymysql  # type: ignore  # noqa: F401
        pytest.skip("pymysql is installed; missing-driver branch not exercised")
    except ImportError:
        pass

    ctx = _ctx(tmp_path)
    vault_ref = f"grants/{uuid.uuid4()}"
    ctx.vault.put(str(ctx.tenant_id), vault_ref, {"db_password": "secret"})
    ctx.granted_capabilities = {
        CAP_REF: {"primary": {"vault_ref": vault_ref, "input_defaults": {}}}
    }

    with pytest.raises(RuntimeError, match="pymysql"):
        await handler(
            ctx,
            {
                "engine": "mysql",
                "dsn": "db.local/app?user=svc",
                "sql": "SELECT 1",
                "account_alias": "primary",
            },
        )


# --------------------------------------------------------------------------
# Definition + pure helpers
# --------------------------------------------------------------------------


def test_definition_shape() -> None:
    assert definition.ref == CAP_REF == "cap.db_query"
    assert "data" in definition.tags
    assert [s.name for s in definition.secrets] == ["db_password"]
    with pytest.raises(ValidationError):
        definition.input_schema(dsn="x", sql="SELECT 1", bogus=1)


def test_input_schema_rejects_bad_engine() -> None:
    with pytest.raises(ValidationError):
        definition.input_schema(engine="oracle", dsn="x", sql="SELECT 1")


def test_input_schema_rejects_max_rows_over_ceiling() -> None:
    with pytest.raises(ValidationError):
        definition.input_schema(dsn="x", sql="SELECT 1", max_rows=999_999)


def test_normalize_params() -> None:
    assert _normalize_params(None) == ()
    assert _normalize_params([1, "a"]) == (1, "a")
    assert _normalize_params((2,)) == (2,)
    assert _normalize_params(5) == (5,)


def test_parse_net_dsn_full() -> None:
    parts = _parse_net_dsn("db.example.com:5432/appdb?user=svc&sslmode=require")
    assert parts["host"] == "db.example.com"
    assert parts["port"] == 5432
    assert parts["dbname"] == "appdb"
    assert parts["user"] == "svc"
    assert parts["query"] == {"sslmode": "require"}


def test_parse_net_dsn_strips_scheme_and_userinfo() -> None:
    parts = _parse_net_dsn("postgres://svc@db.local/appdb")
    assert parts["host"] == "db.local"
    assert parts["port"] is None
    assert parts["dbname"] == "appdb"
    assert parts["user"] == "svc"


def test_parse_net_dsn_missing_db_raises() -> None:
    with pytest.raises(ValueError, match="database name"):
        _parse_net_dsn("db.local")


def test_parse_net_dsn_missing_host_raises() -> None:
    with pytest.raises(ValueError, match="host"):
        _parse_net_dsn("/appdb")


def test_rows_to_dicts() -> None:
    assert _rows_to_dicts(["a", "b"], [(1, 2), (3, 4)]) == [
        {"a": 1, "b": 2},
        {"a": 3, "b": 4},
    ]
