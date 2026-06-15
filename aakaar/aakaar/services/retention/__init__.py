"""Data retention, legal hold, and right-to-erasure.

Banking workloads must (a) not keep PII past a retention window and (b) honour
a customer's right-to-erasure — while ALSO (c) preserving the immutable audit
trail and (d) freezing anything under a litigation/investigation *legal hold*.
Those pull in opposite directions, so this package is deliberately explicit
about what it scrubs and what it must never touch.

Pieces:

  - :class:`~aakaar.services.retention.service.RetentionService` — reads a
    tenant's :class:`RetentionPolicy` per resource type and performs:
      * a **sweep** that erases resources whose reference timestamp is older
        than ``ttl_days`` — SKIPPING anything with ``legal_hold`` set;
      * **right-to-erasure** of one specific resource on demand;
      * **legal-hold** set/clear, which blocks erasure while set.

  - Resource coverage: runs (``inputs``/``outputs``/``error`` + the denormalized
    ``run_events`` payloads) and stored objects (the bytes in the object store).

Erasure is a *tombstone*, not a delete: the row stays (``erased_at`` is set,
StoredObject ``status`` flips to ``erased``) so the audit trail still references
something real. Every erasure is recorded through the audit recorder. The audit
log itself is NEVER a sweepable/erasable resource here — destroying it would
defeat the tamper-evident chain it exists to provide.
"""

from __future__ import annotations

from aakaar.services.retention.service import (
    ERASABLE_RESOURCE_TYPES,
    RESOURCE_RUN,
    RESOURCE_STORED_OBJECT,
    EraseResult,
    LegalHoldError,
    PolicyView,
    RetentionError,
    RetentionService,
    SweepReport,
    UnknownResourceError,
)

__all__ = [
    "ERASABLE_RESOURCE_TYPES",
    "RESOURCE_RUN",
    "RESOURCE_STORED_OBJECT",
    "EraseResult",
    "LegalHoldError",
    "PolicyView",
    "RetentionError",
    "RetentionService",
    "SweepReport",
    "UnknownResourceError",
]
