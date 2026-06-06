"""Append-only audit file sink.

The canonical audit record lives in the ``audit_log`` table; this sink mirrors
each record as one JSON line on disk for tamper-evidence / long-term retention
on the airgapped host (no external log shipper). Best-effort by design: a sink
failure must never break the request that produced the audit event.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AuditFileSink:
    """Appends audit records to ``{root}/audit/audit.jsonl`` (one JSON object
    per line). Thread-safe; swallows all errors."""

    def __init__(self, root: Path | str) -> None:
        self._dir = Path(root) / "audit"
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("audit sink: could not create %s", self._dir, exc_info=True)
        self._path = self._dir / "audit.jsonl"
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def write(self, record: dict[str, Any]) -> None:
        try:
            line = json.dumps(record, default=str, ensure_ascii=False)
            with self._lock, self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:  # pragma: no cover - sink must never break a request
            logger.debug("audit file sink write failed", exc_info=True)


__all__ = ["AuditFileSink"]
