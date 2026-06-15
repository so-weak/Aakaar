"""Audit recorder.

Records tenant-scoped audit events to the ``audit_log`` table (the canonical
store) and mirrors them to a structured log line and an append-only file sink.
Each ``record`` call commits in its own short session so callers don't thread a
transaction through — auditing must never break the action it describes.

Tenant-scoped rows are written into a per-tenant HASH CHAIN: each row gets the
next monotonic ``seq`` and an ``entry_hash`` = sha256 over the previous row's
hash plus this row's immutable fields (see :mod:`aakaar.services.audit.chain`).
That makes the log tamper-evident — editing, deleting, or reordering any
historical row breaks the recomputed chain, which ``/audit/verify`` surfaces.

Concurrency: the app is single-process/single-writer, but two requests for the
same tenant could still interleave the read-max-seq + insert. We serialize that
critical section with a per-tenant in-process lock, and the
``uq_audit_tenant_seq`` unique index is the durable backstop — on a torn-chain
collision the insert is retried with a freshly read seq.

System rows (``tenant_id is None`` — superuser/bootstrap) are NOT chained: they
get a NULL ``seq`` and stay an unverifiable, append-only side log, exactly as
before. Legacy rows written before this change keep their NULL ``seq`` too and
form the pre-chain prefix; the chain begins at the first hashed row.

Sensitive payload fields are redacted before anything is persisted.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from aakaar.db.models import AuditLog
from aakaar.db.session import SessionFactory
from aakaar.services.audit.chain import compute_entry_hash
from aakaar.services.audit.sink import AuditFileSink

logger = logging.getLogger(__name__)

# Bound on how many times a torn-chain unique-violation is retried before the
# write is abandoned (and logged, never raised). In a single-writer process the
# in-process lock makes a collision essentially impossible; this only guards the
# pathological multi-writer case so a single bad row can't spin forever.
_MAX_CHAIN_RETRIES = 5

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
        # One lock per tenant guards the read-max-seq + insert critical section
        # so two concurrent writers for the same tenant can't both claim the
        # same seq (which would either collide on uq_audit_tenant_seq or tear
        # the chain). Created lazily under _locks_guard. System rows (tenant_id
        # is None) are unchained and never enter this section.
        self._locks: dict[uuid.UUID, threading.Lock] = defaultdict(threading.Lock)
        self._locks_guard = threading.Lock()

    def _tenant_lock(self, tenant_id: uuid.UUID) -> threading.Lock:
        with self._locks_guard:
            return self._locks[tenant_id]

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
        action = action[:64]
        target_kind = (target_kind or "system")[:64]
        target_id = (str(target_id) or "-")[:64]
        try:
            if tenant_id is None:
                # System / bootstrap rows are an unverifiable side log: NULL seq,
                # no hash. They never enter the per-tenant chained critical section.
                with self._session_factory.session() as s:
                    s.add(
                        AuditLog(
                            tenant_id=None,
                            actor_id=actor_id,
                            action=action,
                            target_kind=target_kind,
                            target_id=target_id,
                            payload=clean,
                        )
                    )
                    s.commit()
            else:
                self._record_chained(
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    action=action,
                    target_kind=target_kind,
                    target_id=target_id,
                    payload=clean,
                )
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

    def _record_chained(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_id: uuid.UUID | None,
        action: str,
        target_kind: str,
        target_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Append one row to ``tenant_id``'s hash chain.

        Held under the per-tenant lock so reading the tail (max seq + its
        entry_hash) and inserting the successor is atomic for a single process.
        ``at`` is materialized in Python — not left to the column default — so
        the value that is hashed is exactly the value that is stored (the
        verifier rehashes ``row.at``). The ``uq_audit_tenant_seq`` index is the
        durable backstop: a concurrent multi-writer race that slips past the
        lock surfaces as an IntegrityError and is retried with a fresh tail.
        """
        for _attempt in range(_MAX_CHAIN_RETRIES):
            with self._tenant_lock(tenant_id):
                try:
                    with self._session_factory.session() as s:
                        tail = s.execute(
                            select(AuditLog.seq, AuditLog.entry_hash)
                            .where(
                                AuditLog.tenant_id == tenant_id,
                                AuditLog.seq.is_not(None),
                            )
                            .order_by(AuditLog.seq.desc())
                            .limit(1)
                        ).first()
                        if tail is None:
                            next_seq = 1
                            prev_hash: str | None = None
                        else:
                            prev_seq, prev_entry_hash = tail
                            next_seq = int(prev_seq) + 1
                            prev_hash = prev_entry_hash
                        at = datetime.now(UTC)
                        entry_hash = compute_entry_hash(
                            prev_hash=prev_hash,
                            tenant_id=tenant_id,
                            seq=next_seq,
                            actor_id=actor_id,
                            action=action,
                            target_kind=target_kind,
                            target_id=target_id,
                            at=at,
                            payload=payload,
                        )
                        s.add(
                            AuditLog(
                                tenant_id=tenant_id,
                                actor_id=actor_id,
                                action=action,
                                target_kind=target_kind,
                                target_id=target_id,
                                payload=payload,
                                seq=next_seq,
                                # Genesis stores NULL prev_hash; the verifier
                                # substitutes GENESIS_PREV when rehashing it.
                                prev_hash=prev_hash,
                                entry_hash=entry_hash,
                                at=at,
                            )
                        )
                        s.commit()
                    return
                except IntegrityError:
                    # Another writer claimed next_seq first; re-read the tail.
                    logger.warning(
                        "audit chain seq collision for tenant=%s, retrying", tenant_id
                    )
                    continue
        logger.error(
            "audit chain write abandoned after %d retries for tenant=%s action=%s",
            _MAX_CHAIN_RETRIES,
            tenant_id,
            action,
        )


__all__ = ["AuditRecorder"]
