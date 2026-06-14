"""SQLite engines get WAL journaling + a busy timeout (and keep FK enforcement)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from aakaar.db.session import EngineConfig, make_engine


def test_sqlite_file_engine_applies_wal_and_busy_timeout(tmp_path: Path) -> None:
    engine = make_engine(EngineConfig(url=f"sqlite:///{tmp_path / 'wal.sqlite'}"))
    try:
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
            assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
    finally:
        engine.dispose()


def test_sqlite_memory_engine_still_connects(tmp_path: Path) -> None:
    # WAL doesn't apply to :memory: databases; the pragma must not break them.
    engine = make_engine(EngineConfig(url="sqlite://"))
    try:
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA journal_mode")).scalar() == "memory"
            assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000
    finally:
        engine.dispose()
