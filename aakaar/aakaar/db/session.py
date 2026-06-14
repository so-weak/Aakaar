"""Engine + session factory.

Two supported dialects:
  - sqlite (dev / single-node)
  - postgresql+psycopg (Yugabyte / Postgres)

The choice is driven by the URL string. Drivers must be installed by the
caller (sqlite is in stdlib; psycopg ships separately).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from aakaar.db.tenancy import rls_marker

logger = logging.getLogger(__name__)

_RLS_INFO_KEY = "aakaar_rls_marker"


@dataclass(frozen=True, slots=True)
class EngineConfig:
    url: str
    echo: bool = False
    rls_strict: bool = False
    """Mirrored into the per-statement RLS marker resolution (Postgres only).
    See `Settings.rls_strict`."""


def make_engine(config: EngineConfig) -> Engine:
    """Build an Engine and apply per-dialect tweaks.

    SQLite gets foreign-key enforcement turned on (off by default in libsqlite),
    and a connection-level pragma to make multi-thread test runs less painful.

    Postgres gets the Row-Level-Security GUC bridge: before every statement we
    push the active tenancy scope (a tenant UUID, ``"system"``, or ``""``) into
    the transaction-local `app.tenant_id` setting that RLS policies read. We do
    this in `before_cursor_execute` (not just at transaction begin) so a scope
    entered *after* the transaction started — the normal request flow, where
    `get_current_user` runs before the endpoint enters `tenant_scope` — is
    still reflected. A small `connection.info` cache avoids re-issuing the set
    when the marker is unchanged; the `begin` listener clears it because
    `SET LOCAL`/`set_config(...,true)` only lives for one transaction.
    """
    is_sqlite = config.url.startswith("sqlite")
    connect_args: dict[str, object] = {}
    if is_sqlite:
        connect_args["check_same_thread"] = False
    logger.info("db: creating engine dialect=%s echo=%s", "sqlite" if is_sqlite else "other", config.echo)
    engine = create_engine(config.url, echo=config.echo, future=True, connect_args=connect_args)
    if is_sqlite:
        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys = ON")
            # WAL lets concurrent readers proceed during a write (the API +
            # scheduler + workers share one file DB); busy_timeout makes a
            # writer wait out a short lock instead of failing immediately
            # with "database is locked". Both are no-ops on :memory: DBs.
            cur.execute("PRAGMA journal_mode = WAL")
            cur.execute("PRAGMA busy_timeout = 5000")
            cur.close()
    elif engine.dialect.name == "postgresql":
        strict = config.rls_strict

        @event.listens_for(engine, "begin")
        def _rls_clear_on_begin(conn):  # type: ignore[no-untyped-def]
            # set_config(..., is_local=true) resets at transaction end; drop the
            # cache so the next transaction's first statement re-applies it.
            conn.info.pop(_RLS_INFO_KEY, None)

        @event.listens_for(engine, "before_cursor_execute")
        def _rls_set_guc(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
            marker = rls_marker(strict=strict)
            if conn.info.get(_RLS_INFO_KEY) == marker:
                return
            # Use a throwaway DBAPI cursor so we don't disturb the cursor about
            # to run the real statement; set_config takes a bind parameter, so
            # the marker can never be interpolated as SQL.
            c = conn.connection.cursor()
            try:
                c.execute("SELECT set_config('app.tenant_id', %s, true)", (marker,))
            finally:
                c.close()
            conn.info[_RLS_INFO_KEY] = marker

    return engine


class SessionFactory:
    """Lightweight wrapper around `sessionmaker` that surfaces a single
    `session()` context manager."""

    def __init__(self, engine: Engine) -> None:
        self._sm = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    def session(self) -> Session:
        return self._sm()
