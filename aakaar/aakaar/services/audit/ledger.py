"""Read-side of the tamper-evident audit ledger: verify + export.

The write-side (assigning ``seq``/``prev_hash``/``entry_hash``) lives in
:mod:`aakaar.services.audit.recorder`; the hashing itself is in
:mod:`aakaar.services.audit.chain`. This module recomputes the chain a tenant's
rows form so an auditor can prove non-tampering (verify) or archive the chain
for external attestation (export).

Both functions are pure reads scoped to a single tenant. Legacy rows with a
NULL ``seq`` are the *pre-chain prefix*: they predate hashing and are skipped
here (the chain begins at the first row that has a ``seq``). History is never
rewritten — verify only reports, it never repairs.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from aakaar.db.models import AuditLog
from aakaar.services.audit.chain import GENESIS_PREV, compute_entry_hash


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Outcome of recomputing a tenant's chain.

    ``intact`` is True iff every hashed row's recomputed ``entry_hash`` matches
    what is stored AND the ``prev_hash`` links are continuous. ``broken_at`` is
    the ``seq`` of the first row that fails (None when intact). ``count`` /
    ``first_seq`` / ``last_seq`` describe the hashed segment that was checked.
    """

    intact: bool
    count: int
    first_seq: int | None
    last_seq: int | None
    broken_at: int | None
    reason: str | None


def _chained_rows(session: Session, tenant_id: uuid.UUID) -> list[AuditLog]:
    """Hashed rows for a tenant, ordered by ``seq`` (the chain order).

    Rows with a NULL ``seq`` (pre-chain / legacy) are excluded — they are not
    part of the verifiable chain.
    """
    stmt = (
        select(AuditLog)
        .where(AuditLog.tenant_id == tenant_id, AuditLog.seq.is_not(None))
        .order_by(AuditLog.seq.asc())
    )
    return list(session.scalars(stmt))


def verify_chain(session: Session, *, tenant_id: uuid.UUID) -> VerifyResult:
    """Recompute ``tenant_id``'s hash chain and report the first broken link.

    Detects: a tampered immutable field (recomputed ``entry_hash`` differs), a
    severed link (stored ``prev_hash`` does not match the predecessor's
    ``entry_hash``), a deleted/reordered row (a gap or non-monotonic ``seq``),
    and a missing stored hash on a sequenced row.
    """
    rows = _chained_rows(session, tenant_id)
    if not rows:
        return VerifyResult(
            intact=True,
            count=0,
            first_seq=None,
            last_seq=None,
            broken_at=None,
            reason=None,
        )

    expected_prev: str | None = None  # predecessor's entry_hash; None => genesis
    expected_seq = rows[0].seq  # chain may legitimately start at >1 (legacy prefix)
    first_seq = rows[0].seq
    for row in rows:
        seq = row.seq
        assert seq is not None  # _chained_rows filters NULLs
        # Continuity: seq must increment by exactly one with no gaps/reorders.
        if seq != expected_seq:
            return VerifyResult(
                intact=False,
                count=len(rows),
                first_seq=first_seq,
                last_seq=rows[-1].seq,
                broken_at=seq,
                reason=f"sequence gap or reorder: expected seq={expected_seq}, found {seq}",
            )
        if row.entry_hash is None:
            return VerifyResult(
                intact=False,
                count=len(rows),
                first_seq=first_seq,
                last_seq=rows[-1].seq,
                broken_at=seq,
                reason="row is sequenced but has no entry_hash",
            )
        # Link integrity: the stored prev_hash must equal the predecessor's
        # entry_hash (genesis carries the sentinel / NULL).
        stored_prev = row.prev_hash if row.prev_hash is not None else None
        link_prev = GENESIS_PREV if expected_prev is None else expected_prev
        if (stored_prev or GENESIS_PREV) != link_prev:
            return VerifyResult(
                intact=False,
                count=len(rows),
                first_seq=first_seq,
                last_seq=rows[-1].seq,
                broken_at=seq,
                reason="prev_hash does not match predecessor entry_hash",
            )
        # Tamper check: recompute over the immutable fields + the chained prev.
        recomputed = compute_entry_hash(
            prev_hash=row.prev_hash,
            tenant_id=tenant_id,
            seq=seq,
            actor_id=row.actor_id,
            action=row.action,
            target_kind=row.target_kind,
            target_id=row.target_id,
            at=row.at,
            payload=row.payload,
        )
        if recomputed != row.entry_hash:
            return VerifyResult(
                intact=False,
                count=len(rows),
                first_seq=first_seq,
                last_seq=rows[-1].seq,
                broken_at=seq,
                reason="entry_hash mismatch (a covered field was altered)",
            )
        expected_prev = row.entry_hash
        expected_seq = seq + 1

    return VerifyResult(
        intact=True,
        count=len(rows),
        first_seq=first_seq,
        last_seq=rows[-1].seq,
        broken_at=None,
        reason=None,
    )


def iter_chain(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    after_seq: int | None = None,
    limit: int = 1000,
) -> Iterator[AuditLog]:
    """Yield a tenant's hashed rows in chain order, ``seq`` ascending.

    Bounded by ``limit`` and resumable via ``after_seq`` (exclusive) so the
    export endpoint can page through a large ledger without loading it all.
    Only sequenced (chained) rows are returned.
    """
    stmt = (
        select(AuditLog)
        .where(AuditLog.tenant_id == tenant_id, AuditLog.seq.is_not(None))
        .order_by(AuditLog.seq.asc())
        .limit(limit)
    )
    if after_seq is not None:
        stmt = stmt.where(AuditLog.seq > after_seq)
    yield from session.scalars(stmt)


__all__ = ["VerifyResult", "iter_chain", "verify_chain"]
