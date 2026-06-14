"""AAKAAR_LOG_FORMAT=json: one-line JSON records with ts/level/logger/msg/exc."""

from __future__ import annotations

import json
import logging

import pytest

from aakaar.core.logging import _JsonFormatter, setup_logging


def _record(level: int = logging.INFO, msg: str = "hello %s", *args, exc_info=None):
    return logging.LogRecord(
        name="aakaar.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )


def test_json_formatter_emits_one_line_with_core_fields() -> None:
    line = _JsonFormatter().format(_record(logging.WARNING, "hello %s", "world"))
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "aakaar.test"
    assert payload["msg"] == "hello world"
    assert payload["ts"].endswith("Z")


def test_json_formatter_includes_exception_on_one_line() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _record(logging.ERROR, "failed", exc_info=sys.exc_info())
    line = _JsonFormatter().format(record)
    assert "\n" not in line
    payload = json.loads(line)
    assert "ValueError: boom" in payload["exc"]


def test_json_formatter_surfaces_context_extras() -> None:
    record = _record()
    record.run_id = "r-1"
    record.tenant_id = "t-1"
    payload = json.loads(_JsonFormatter().format(record))
    assert payload["run_id"] == "r-1"
    assert payload["tenant_id"] == "t-1"


def test_setup_logging_selects_json_formatter(monkeypatch: pytest.MonkeyPatch) -> None:
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        monkeypatch.setenv("AAKAAR_LOG_FORMAT", "json")
        setup_logging(force=True)
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, _JsonFormatter)

        monkeypatch.setenv("AAKAAR_LOG_FORMAT", "text")
        setup_logging(force=True)
        assert not isinstance(root.handlers[0].formatter, _JsonFormatter)
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
