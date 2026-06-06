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

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EngineConfig:
    url: str
    echo: bool = False


def make_engine(config: EngineConfig) -> Engine:
    """Build an Engine and apply per-dialect tweaks.

    SQLite gets foreign-key enforcement turned on (off by default in libsqlite),
    and a connection-level pragma to make multi-thread test runs less painful.
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
            cur.close()

    return engine


class SessionFactory:
    """Lightweight wrapper around `sessionmaker` that surfaces a single
    `session()` context manager."""

    def __init__(self, engine: Engine) -> None:
        self._sm = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    def session(self) -> Session:
        return self._sm()
