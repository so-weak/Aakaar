"""Central logging configuration for the Aakaar backend.

`setup_logging()` is the one place that touches the root logger and
third-party logger levels. Modules across the codebase get their own
named logger via `logging.getLogger(__name__)` (e.g. `aakaar.api.routers.runs`)
and call into it without thinking about formatters or handlers.

Environment variables
---------------------
- AAKAAR_LOG_LEVEL: root level (default INFO). DEBUG | INFO | WARNING | ERROR.
- AAKAAR_LOG_FORMAT: "text" (default, human-readable) or "json".
- AAKAAR_LOG_LEVELS: comma-separated overrides for specific loggers, e.g.
  "aakaar.planner=DEBUG,aakaar.workers.browser=DEBUG,sqlalchemy.engine=INFO".
- AAKAAR_LOG_NOISY_THIRD_PARTY: "quiet" (default) caps a known list of chatty
  third-party loggers at WARNING; "verbose" leaves them at the root level.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

_CONFIGURED = False

# Third-party libraries we depend on whose default INFO/DEBUG output is more
# noise than signal in normal operation. Capped at WARNING unless the user
# opts into "verbose".
_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "urllib3",
    "openai",
    "openai._base_client",
    "sqlalchemy.engine",
    "sqlalchemy.pool",
    "alembic",
    "chromadb",
    "chromadb.telemetry",
    "chromadb.segment",
    "sentence_transformers",
    "transformers",
    "huggingface_hub",
    "playwright",
    "asyncio",
    "watchfiles",
    "uvicorn.access",
)


class _JsonFormatter(logging.Formatter):
    """Minimal JSON line formatter — enough for grep/jq pipelines."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Surface common contextual extras when present without forcing every
        # call site to use the same kwarg names.
        for key in ("run_id", "node_id", "tenant_id", "user_id", "request_id"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, default=str)


def setup_logging(*, force: bool = False) -> None:
    """Configure the root logger. Safe to call multiple times — subsequent
    calls are no-ops unless `force=True` (used by tests that want a clean slate).
    """

    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    level_name = os.environ.get("AAKAAR_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    fmt = os.environ.get("AAKAAR_LOG_FORMAT", "text").lower()
    handler = logging.StreamHandler(stream=sys.stderr)
    # Stamp the active request id (when inside a request) onto every record so
    # the JSON formatter can surface it. Import locally to avoid a hard
    # dependency on the API layer from the logging module.
    from aakaar.core.middleware.request_id import RequestIdFilter

    handler.addFilter(RequestIdFilter())
    if fmt == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    root = logging.getLogger()
    if force:
        for existing in list(root.handlers):
            root.removeHandler(existing)
    # Don't stack handlers on repeat calls — only attach if the root has none.
    if not root.handlers:
        root.addHandler(handler)
    root.setLevel(level)

    # Aakaar's own loggers always emit at the configured root level. We set
    # this explicitly so a stray `logging.basicConfig()` somewhere upstream
    # (e.g. uvicorn) can't silently raise our level.
    logging.getLogger("aakaar").setLevel(level)

    if os.environ.get("AAKAAR_LOG_NOISY_THIRD_PARTY", "quiet").lower() != "verbose":
        for name in _NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)

    overrides = os.environ.get("AAKAAR_LOG_LEVELS", "")
    if overrides:
        for chunk in overrides.split(","):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            name, value = chunk.split("=", 1)
            try:
                logging.getLogger(name.strip()).setLevel(value.strip().upper())
            except (ValueError, TypeError):
                # Bad override — log via the root and keep going.
                logging.getLogger(__name__).warning(
                    "ignoring invalid AAKAAR_LOG_LEVELS entry: %r", chunk
                )

    _CONFIGURED = True
    logging.getLogger(__name__).debug(
        "logging configured (level=%s format=%s)", level_name, fmt
    )


__all__ = ["setup_logging"]
