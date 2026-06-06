"""Audit recorder.

Records tenant-scoped audit events to the ``audit_log`` table (the canonical
store) and mirrors them to a structured log line and an append-only file sink.
The DB write is added to the CALLER's session so the audit row commits
atomically with the action it describes — if the action rolls back, so does
its audit record.

Sensitive payload fields are redacted before anything is persisted.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from aakaar.db.models import AuditLog
from aakaar.db.session import SessionFactory
from aakaar.services.audit.sink import AuditFileSink

logger = logging.getLogger(__name__)

# Mirrors the executor/orchestrator redaction set, plus auth-specific keys.
_REDACT_KEYS = {
    "password",
    "token",
    "api_key",
    "secret",
    "authorization",
    "totp_secret",
    "secret_id",
    "client_secret",
    "private_key",
}


def _redact(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {
        k: ("<redacted>" if k.lower() in _REDACT_KEYS else v) for k, v in payload.items()
    }


class AuditRecorder:
    """Writes audit events. Construct once and share. Each ``record`` call
    commits in its own short session so routers don't have to thread their
    transaction through — auditing is best-effort and must never break the
    action it describes (a failed audit write is logged, not raised)."""

    def __init__(
        self, session_factory: SessionFactory, sink: AuditFileSink | None = None
    ) -> None:
        self._session_factory = session_factory
        self._sink = sink

    def record(
        self,
        *,
        action: str,
        tenant_id: uuid.UUID | None = None,
        actor_id: uuid.UUID | None = None,
        target_kind: str = "system",
        target_id: str = "-",
        payload: dict[str, Any] | None = None,
    ) -> None:
        clean = _redact(payload)
        try:
            with self._session_factory.session() as s:
                s.add(
                    AuditLog(
                        tenant_id=tenant_id,
                        actor_id=actor_id,
                        action=action[:64],
                        target_kind=(target_kind or "system")[:64],
                        target_id=(str(target_id) or "-")[:64],
                        payload=clean,
                    )
                )
                s.commit()
        except Exception:  # pragma: no cover - audit must never break a request
            logger.warning("audit DB write failed for action=%s", action, exc_info=True)

        if self._sink is not None:
            self._sink.write(
                {
                    "at": datetime.now(UTC).isoformat(),
                    "action": action,
                    "tenant_id": str(tenant_id) if tenant_id else None,
                    "actor_id": str(actor_id) if actor_id else None,
                    "target_kind": target_kind,
                    "target_id": str(target_id),
                    "payload": clean,
                }
            )
        logger.info(
            "audit action=%s tenant=%s actor=%s target=%s/%s",
            action,
            tenant_id,
            actor_id,
            target_kind,
            target_id,
        )


__all__ = ["AuditRecorder"]
