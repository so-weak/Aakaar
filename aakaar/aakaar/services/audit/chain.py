"""Hash-chain primitives for the tamper-evident audit ledger.

Every tenant-scoped audit row carries a per-tenant monotonic ``seq`` and an
``entry_hash`` = sha256 over a CANONICAL serialization of the immutable audit
fields plus the previous row's ``entry_hash``. Linking each entry to its
predecessor makes the log tamper-evident: editing any historical field (or
deleting/reordering a row) breaks the recomputed chain at that point, which the
``/audit/verify`` endpoint surfaces to an auditor.

The serialization here is the single source of truth — the writer and the
verifier both call :func:`compute_entry_hash`, so they can never disagree about
what a row's hash "should" be. Keep it deterministic and append-only-stable:
never reorder the field list or change the encoding, or every historical hash
would change and the whole chain would appear broken.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

# Genesis sentinel: the value fed in as ``prev_hash`` for the first entry of a
# tenant's chain (seq == 1). Distinct from the empty string so a NULL prev_hash
# row and a "" prev_hash row can never collide.
GENESIS_PREV = ""


def _canonical_at(at: datetime) -> str:
    """Stable string form of an audit timestamp, independent of how the DB
    round-trips tzinfo.

    SQLite's ``DateTime(timezone=True)`` stores the wall-clock and drops the
    tzinfo, so a value hashed at write time as aware UTC comes back naive. We
    normalize to UTC and emit a fixed microsecond-precision form so the writer
    and the verifier (which rehashes the value read back from the DB) always
    agree. Naive datetimes are assumed to already be UTC — the recorder only
    ever writes ``datetime.now(UTC)``.
    """
    if at.tzinfo is not None:
        at = at.astimezone(UTC).replace(tzinfo=None)
    return at.isoformat(timespec="microseconds")


def canonical_payload(
    *,
    tenant_id: uuid.UUID,
    seq: int,
    actor_id: uuid.UUID | None,
    action: str,
    target_kind: str,
    target_id: str,
    at: datetime,
    payload: dict[str, Any] | None,
) -> str:
    """Deterministic string covering exactly the IMMUTABLE audit fields.

    Uses sorted-key JSON with no insignificant whitespace so the bytes are
    reproducible across processes and Python versions. ``payload`` is embedded
    as a nested object (also sorted) so a regulator can recompute the hash from
    the exported JSON without guessing at field order.
    """
    body = {
        "tenant_id": str(tenant_id),
        "seq": seq,
        "actor_id": str(actor_id) if actor_id is not None else None,
        "action": action,
        "target_kind": target_kind,
        "target_id": target_id,
        # Normalized so the DB tz round-trip can't change the hashed bytes.
        "at": _canonical_at(at),
        "payload": payload if payload is not None else {},
    }
    return json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def compute_entry_hash(
    *,
    prev_hash: str | None,
    tenant_id: uuid.UUID,
    seq: int,
    actor_id: uuid.UUID | None,
    action: str,
    target_kind: str,
    target_id: str,
    at: datetime,
    payload: dict[str, Any] | None,
) -> str:
    """Hex sha256 over ``prev_hash || canonical_payload(...)``.

    ``prev_hash`` is the predecessor's ``entry_hash`` (or :data:`GENESIS_PREV`
    for seq == 1). Including it is what chains the entries together.
    """
    material = (prev_hash or GENESIS_PREV) + "\n" + canonical_payload(
        tenant_id=tenant_id,
        seq=seq,
        actor_id=actor_id,
        action=action,
        target_kind=target_kind,
        target_id=target_id,
        at=at,
        payload=payload,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


__all__ = ["GENESIS_PREV", "canonical_payload", "compute_entry_hash"]
