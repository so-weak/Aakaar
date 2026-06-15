"""cap.db_query — run a parameterized SQL query against a relational database.

Server-local SQL fetch capability. Supports three engines:

  sqlite   — stdlib `sqlite3`. The `dsn` is either a filesystem path the
             worker can read, or a managed-storage `aakaar://...` URI. When
             a URI is given the bytes are materialized to a temp file,
             opened read-only, and the temp file is removed afterwards.
  postgres — lazy `psycopg` (v3). Connects to host/db named in the `dsn`.
  mysql    — lazy `pymysql`. Connects to host/db named in the `dsn`.

For postgres/mysql the password is read fresh from the tenant vault under
the supplied `account_alias` (secret name `db_password`); it is never
logged or returned. sqlite is file-local and needs no credentials.

The query is always executed with bound `params` — never string-formatted
into the SQL — so the capability cannot be coerced into SQL injection from
upstream node data. Positional params bind to `?`/`%s` placeholders.

Results are capped at `max_rows` (default 500, hard ceiling 50000) so a
runaway query can't blow up worker memory. Returns:

    {
      "columns": ["id", "name", ...],
      "rows":    [ {"id": 1, "name": "a"}, ... ],
      "rowcount": <number of rows returned>,
    }

Non-SELECT statements (INSERT/UPDATE/...) return an empty `rows`/`columns`
and `rowcount` reflecting the driver's affected-row count where available.
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from pydantic import BaseModel, ConfigDict, Field

from aakaar.interpreter.activities.types import ActivityContext
from aakaar.interpreter.credentials import fetch_credentials
from aakaar.shared.registry import CapabilityDefinition, SecretSpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.db_query"

_DEFAULT_MAX_ROWS = 500
_MAX_ROWS_CEILING = 50_000


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engine: str = Field(
        default="sqlite",
        pattern="^(sqlite|postgres|mysql)$",
        description="Database engine: 'sqlite' (default), 'postgres', or 'mysql'.",
    )
    dsn: str = Field(
        description=(
            "For sqlite: a filesystem path the worker can read, or a managed-"
            "storage 'aakaar://...' URI to a .sqlite/.db file. For postgres/"
            "mysql: a connection string of the form "
            "'host[:port]/dbname?user=...&sslmode=...' (no password — that "
            "comes from the vault via account_alias)."
        ),
    )
    sql: str = Field(description="The SQL statement to execute. Use placeholders for params.")
    params: list[Any] | None = Field(
        default=None,
        description=(
            "Optional positional bind parameters for the placeholders in `sql` "
            "('?' for sqlite, '%s' for postgres/mysql). Never string-formatted "
            "into the query."
        ),
    )
    account_alias: str | None = Field(
        default=None,
        description=(
            "Vault grant alias supplying the 'db_password' secret. Required for "
            "postgres/mysql; ignored for sqlite."
        ),
    )
    max_rows: int = Field(
        default=_DEFAULT_MAX_ROWS,
        ge=1,
        le=_MAX_ROWS_CEILING,
        description="Maximum number of rows to fetch (hard ceiling 50000).",
    )


class _Outputs(BaseModel):
    columns: list[str] = Field(description="Column names in result order.")
    rows: list[dict[str, Any]] = Field(description="Result rows as column->value dicts.")
    rowcount: int = Field(description="Number of rows returned (or affected, for non-SELECT).")


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Run a parameterized SQL query against a sqlite, postgres, or mysql "
        "database and return the result rows. sqlite reads a local file or a "
        "managed-storage URI; postgres/mysql connect over the network using a "
        "password fetched from the vault. Params are always bound, never "
        "interpolated; results are capped at max_rows."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(
        SecretSpec(
            name="db_password",
            description=(
                "Database password for postgres/mysql. Not used for sqlite; "
                "fetched fresh per run and never logged or returned."
            ),
        ),
    ),
    tags=("data", "sql", "database"),
)


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def _normalize_params(params: Any) -> tuple[Any, ...]:
    """Coerce the optional params input into a tuple suitable for the DB
    driver's positional binding. None -> empty tuple."""
    if params is None:
        return ()
    if isinstance(params, (list, tuple)):
        return tuple(params)
    # A scalar single param is tolerated.
    return (params,)


def _parse_net_dsn(dsn: str) -> dict[str, Any]:
    """Parse a 'host[:port]/dbname?user=...&...' DSN for postgres/mysql.

    A leading scheme ('postgres://', 'mysql://') is tolerated and stripped.
    Returns a dict with host, port (or None), dbname, user (or None), and a
    `query` dict of any remaining querystring options. Raises ValueError if
    host or dbname is missing.
    """
    raw = dsn.strip()
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    # urlparse needs a scheme to populate netloc; give it a synthetic one.
    parsed = urlparse("scheme://" + raw)
    host = parsed.hostname
    if not host:
        raise ValueError(f"db_query: DSN missing host: {dsn!r}")
    dbname = parsed.path.lstrip("/")
    if not dbname:
        raise ValueError(f"db_query: DSN missing database name: {dsn!r}")
    qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    user = parsed.username or qs.pop("user", None) or qs.pop("username", None)
    return {
        "host": host,
        "port": parsed.port,
        "dbname": unquote(dbname),
        "user": user,
        "query": qs,
    }


def _rows_to_dicts(
    columns: list[str], rows: list[tuple[Any, ...]]
) -> list[dict[str, Any]]:
    return [dict(zip(columns, row, strict=False)) for row in rows]


# --------------------------------------------------------------------------
# Engine runners — each returns (columns, rows-as-tuples, rowcount)
# --------------------------------------------------------------------------


def _run_sqlite(
    ctx: ActivityContext, dsn: str, sql: str, params: tuple[Any, ...], max_rows: int
) -> tuple[list[str], list[tuple[Any, ...]], int]:
    import sqlite3

    tmp_path: str | None = None
    db_path = dsn
    if dsn.startswith("aakaar://"):
        data = ctx.object_store.get(dsn)
        fd, tmp_path = tempfile.mkstemp(prefix="aakaar-db-", suffix=".sqlite")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        except Exception:
            os.unlink(tmp_path)
            raise
        db_path = tmp_path

    try:
        # Open read-only via URI so a query can't mutate a materialized copy
        # in surprising ways and to fail fast on a missing local file.
        if db_path.startswith("file:"):
            uri = db_path
            use_uri = True
        else:
            uri = "file:" + os.path.abspath(db_path) + "?mode=ro"
            use_uri = True
        conn = sqlite3.connect(uri, uri=use_uri)
        try:
            cur = conn.execute(sql, params)
            if cur.description is None:
                # Non-SELECT (DDL/DML). Read-only mode rejects writes, so
                # this path is mostly DDL-less pragmas; report rowcount.
                return [], [], max(cur.rowcount, 0)
            columns = [d[0] for d in cur.description]
            fetched = cur.fetchmany(max_rows)
            return columns, [tuple(r) for r in fetched], len(fetched)
        finally:
            conn.close()
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                logger.debug("db_query: temp sqlite file already gone: %s", tmp_path)


def _run_postgres(
    password: str | None,
    dsn: str,
    sql: str,
    params: tuple[Any, ...],
    max_rows: int,
) -> tuple[list[str], list[tuple[Any, ...]], int]:
    try:
        import psycopg
    except ImportError as e:
        raise RuntimeError(
            "cap.db_query: engine 'postgres' requires the 'psycopg' package, "
            "which is not installed on this worker"
        ) from e

    parts = _parse_net_dsn(dsn)
    conn_kwargs: dict[str, Any] = {
        "host": parts["host"],
        "dbname": parts["dbname"],
    }
    if parts["port"]:
        conn_kwargs["port"] = parts["port"]
    if parts["user"]:
        conn_kwargs["user"] = parts["user"]
    if password:
        conn_kwargs["password"] = password
    conn_kwargs.update(parts["query"])

    with psycopg.connect(**conn_kwargs) as conn, conn.cursor() as cur:
        cur.execute(sql, params or None)
        if cur.description is None:
            return [], [], max(cur.rowcount, 0)
        columns = [d[0] for d in cur.description]
        fetched = cur.fetchmany(max_rows)
        return columns, [tuple(r) for r in fetched], len(fetched)


def _run_mysql(
    password: str | None,
    dsn: str,
    sql: str,
    params: tuple[Any, ...],
    max_rows: int,
) -> tuple[list[str], list[tuple[Any, ...]], int]:
    try:
        import pymysql  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "cap.db_query: engine 'mysql' requires the 'pymysql' package, "
            "which is not installed on this worker"
        ) from e

    parts = _parse_net_dsn(dsn)
    conn_kwargs: dict[str, Any] = {
        "host": parts["host"],
        "database": parts["dbname"],
    }
    if parts["port"]:
        conn_kwargs["port"] = int(parts["port"])
    if parts["user"]:
        conn_kwargs["user"] = parts["user"]
    if password:
        conn_kwargs["password"] = password

    conn = pymysql.connect(**conn_kwargs)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or None)
            if cur.description is None:
                return [], [], max(cur.rowcount, 0)
            columns = [d[0] for d in cur.description]
            fetched = cur.fetchmany(max_rows)
            return columns, [tuple(r) for r in fetched], len(fetched)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Handler
# --------------------------------------------------------------------------


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    engine = inputs.get("engine", "sqlite")
    dsn = inputs["dsn"]
    sql = inputs["sql"]
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("cap.db_query: `sql` must be a non-empty string")
    params = _normalize_params(inputs.get("params"))
    max_rows = int(inputs.get("max_rows", _DEFAULT_MAX_ROWS))
    alias = inputs.get("account_alias")

    logger.info(
        "cap.db_query start run_id=%s engine=%s max_rows=%d nparams=%d",
        ctx.run_id,
        engine,
        max_rows,
        len(params),
    )

    if engine == "sqlite":
        columns, rows, rowcount = _run_sqlite(ctx, dsn, sql, params, max_rows)
    else:
        # postgres / mysql require a password from the vault.
        if not alias:
            raise PermissionError(
                f"cap.db_query: engine {engine!r} requires `account_alias` "
                f"supplying the 'db_password' secret"
            )
        creds = fetch_credentials(ctx, capability_ref=CAP_REF, account_alias=alias)
        password = creds.get("db_password") or None
        if engine == "postgres":
            columns, rows, rowcount = _run_postgres(password, dsn, sql, params, max_rows)
        elif engine == "mysql":
            columns, rows, rowcount = _run_mysql(password, dsn, sql, params, max_rows)
        else:  # pragma: no cover - guarded by input schema pattern
            raise ValueError(f"cap.db_query: unsupported engine {engine!r}")

    result_rows = _rows_to_dicts(columns, rows)
    logger.info(
        "cap.db_query ok run_id=%s engine=%s columns=%d rowcount=%d",
        ctx.run_id,
        engine,
        len(columns),
        rowcount,
    )
    return {"columns": columns, "rows": result_rows, "rowcount": rowcount}
