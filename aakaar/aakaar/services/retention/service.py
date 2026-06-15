"""Retention / legal-hold / right-to-erasure service.

Tenant-scoped. Reads ``RetentionPolicy`` rows and acts on the resources they
govern. Two resource types are erasable today:

  - ``run`` — scrubs ``inputs`` / ``outputs`` / ``error`` on the run and the
    redacted payloads mirrored onto its ``run_events``, then stamps
    ``erased_at``. The row remains as an audit tombstone.
  - ``stored_object`` — deletes the underlying bytes via the object store, flips
    ``status`` to ``erased`` and stamps ``erased_at``. The metadata row remains.

Three operations:

  - :meth:`RetentionService.sweep` — for each tenant policy with a finite
    ``ttl_days``, erase resources whose reference timestamp is older than the
    cutoff, EXCEPT any with ``legal_hold`` set or already erased.
  - :meth:`RetentionService.erase_resource` — right-to-erasure for one resource;
    refuses while a legal hold is in force.
  - :meth:`RetentionService.set_legal_hold` — set/clear the hold flag.

Each mutation runs in its own short ``SessionFactory`` transaction (the
``DbEventRecorder`` / ``CheckpointStore`` convention — no session is held across
the audit call or the object-store I/O). Erasure of the object bytes happens
*before* the row is flipped, but a missing object is tolerated (idempotent
re-erasure). Every erasure is recorded via the audit recorder; the audit log is
never itself a target — it must outlive what it describes.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from aakaar.db.models import (
    RetentionPolicy,
    Run,
    RunEvent,
    StoredObject,
    StoredObjectStatus,
)
from aakaar.db.session import SessionFactory
from aakaar.services.audit import AuditRecorder
from aakaar.storage.object_store import ObjectNotFound, ObjectStorage

logger = logging.getLogger(__name__)

# Resource types this service can sweep/erase. These are the values an admin
# puts in RetentionPolicy.resource_type. Other values (e.g. 'audit_log') may
# carry a policy for documentation/reporting but are intentionally NOT erasable
# here — see module docstring.
RESOURCE_RUN = "run"
RESOURCE_STORED_OBJECT = "stored_object"
ERASABLE_RESOURCE_TYPES: frozenset[str] = frozenset({RESOURCE_RUN, RESOURCE_STORED_OBJECT})

# Marker left in place of scrubbed JSON payloads so a reader can tell the
# difference between "empty" and "erased". Kept tiny and non-PII.
_SCRUBBED = {"_erased": True}


def _to_view(policy: RetentionPolicy) -> PolicyView:
    return PolicyView(
        resource_type=policy.resource_type,
        ttl_days=policy.ttl_days,
        updated_at=policy.updated_at,
        updated_by=policy.updated_by,
    )


class RetentionError(Exception):
    """Base class for retention/erasure failures."""


class UnknownResourceError(RetentionError):
    """The resource does not exist in this tenant (cross-tenant => this too)."""


class LegalHoldError(RetentionError):
    """Erasure was attempted on a resource under legal hold."""


@dataclass(frozen=True, slots=True)
class PolicyView:
    """Detached, read-safe snapshot of a :class:`RetentionPolicy` row.

    The service owns its DB sessions (short transactions), so it returns plain
    DTOs rather than ORM rows — a router can read these after the session has
    closed without risking ``DetachedInstanceError``.
    """

    resource_type: str
    ttl_days: int | None
    updated_at: datetime
    updated_by: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class EraseResult:
    resource_type: str
    resource_id: uuid.UUID
    erased_at: datetime
    already_erased: bool = False
    """True when the resource was already a tombstone — the call was a no-op."""


@dataclass(slots=True)
class SweepReport:
    """What one :meth:`RetentionService.sweep` pass did."""

    scanned: int = 0
    erased: int = 0
    skipped_legal_hold: int = 0
    already_erased: int = 0
    by_type: dict[str, int] = field(default_factory=dict)

    def _erased(self, resource_type: str) -> None:
        self.erased += 1
        self.by_type[resource_type] = self.by_type.get(resource_type, 0) + 1


class RetentionService:
    """Performs retention sweeps, right-to-erasure, and legal-hold toggles.

    Construct once and share. ``object_store`` is needed to scrub stored-object
    bytes; ``audit`` records every erasure (best-effort — a failed audit write
    never aborts the erasure, mirroring :class:`AuditRecorder`'s own contract,
    but the erasure itself is the durable record via ``erased_at``).
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        object_store: ObjectStorage,
        audit: AuditRecorder,
    ) -> None:
        self._sf = session_factory
        self._object_store = object_store
        self._audit = audit

    # ---- policy reads -----------------------------------------------------

    def get_policy(
        self, tenant_id: uuid.UUID, resource_type: str
    ) -> PolicyView | None:
        with self._sf.session() as s:
            policy = self._get_policy(s, tenant_id, resource_type)
            return _to_view(policy) if policy is not None else None

    def list_policies(self, tenant_id: uuid.UUID) -> list[PolicyView]:
        with self._sf.session() as s:
            rows = s.scalars(
                select(RetentionPolicy)
                .where(RetentionPolicy.tenant_id == tenant_id)
                .order_by(RetentionPolicy.resource_type)
            )
            return [_to_view(p) for p in rows]

    def upsert_policy(
        self,
        *,
        tenant_id: uuid.UUID,
        resource_type: str,
        ttl_days: int | None,
        updated_by: uuid.UUID | None,
    ) -> PolicyView:
        """Create or update the single policy for (tenant, resource_type).

        ``ttl_days`` may be ``None`` (retain forever) or a positive int. A
        non-positive ttl is rejected — 0 would mean "erase everything the moment
        it's written", almost always a mistake, and we won't let it through a
        sweep silently.
        """
        if ttl_days is not None and ttl_days <= 0:
            raise RetentionError("ttl_days must be a positive integer or null")
        now = datetime.now(UTC)
        with self._sf.session() as s:
            policy = self._get_policy(s, tenant_id, resource_type)
            if policy is None:
                policy = RetentionPolicy(
                    tenant_id=tenant_id,
                    resource_type=resource_type,
                    ttl_days=ttl_days,
                    updated_by=updated_by,
                )
                s.add(policy)
            else:
                policy.ttl_days = ttl_days
                policy.updated_by = updated_by
                policy.updated_at = now
            s.commit()
            s.refresh(policy)
            return _to_view(policy)

    # ---- legal hold -------------------------------------------------------

    def set_legal_hold(
        self,
        *,
        tenant_id: uuid.UUID,
        resource_type: str,
        resource_id: uuid.UUID,
        hold: bool,
        actor_id: uuid.UUID | None = None,
    ) -> None:
        """Set or clear the legal-hold flag on a run or stored object.

        A held resource is skipped by :meth:`sweep` and refused by
        :meth:`erase_resource`. Clearing the hold re-exposes it to retention.
        Raises :class:`UnknownResourceError` if the resource is absent or not in
        this tenant.
        """
        self._require_erasable(resource_type)
        with self._sf.session() as s:
            row = self._load_row(s, resource_type, tenant_id, resource_id)
            if row is None:
                raise UnknownResourceError(
                    f"{resource_type} {resource_id} not found in tenant {tenant_id}"
                )
            row.legal_hold = hold
            s.commit()
        self._audit.record(
            action="retention.legal_hold_set" if hold else "retention.legal_hold_cleared",
            tenant_id=tenant_id,
            actor_id=actor_id,
            target_kind=resource_type,
            target_id=str(resource_id),
            payload={"hold": hold},
        )
        logger.info(
            "retention.legal_hold tenant=%s %s=%s hold=%s",
            tenant_id,
            resource_type,
            resource_id,
            hold,
        )

    # ---- right to erasure -------------------------------------------------

    def erase_resource(
        self,
        *,
        tenant_id: uuid.UUID,
        resource_type: str,
        resource_id: uuid.UUID,
        actor_id: uuid.UUID | None = None,
        reason: str = "",
    ) -> EraseResult:
        """Erase one resource on demand (right-to-erasure).

        Refuses with :class:`LegalHoldError` while a hold is in force —
        litigation/investigation holds outrank an erasure request. Idempotent:
        erasing an already-erased resource is a no-op that still returns a
        result. Raises :class:`UnknownResourceError` for an absent/cross-tenant
        resource and :class:`UnknownResourceError` (via ``_require_erasable``)
        for an unsupported type.
        """
        self._require_erasable(resource_type)
        result = self._erase_one(
            tenant_id=tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        if result is None:
            raise UnknownResourceError(
                f"{resource_type} {resource_id} not found in tenant {tenant_id}"
            )
        # Audit only a real erasure (not a no-op repeat) so the trail isn't
        # padded with duplicate entries on retried requests.
        if not result.already_erased:
            self._audit.record(
                action="retention.erased",
                tenant_id=tenant_id,
                actor_id=actor_id,
                target_kind=resource_type,
                target_id=str(resource_id),
                payload={"reason": reason or "right_to_erasure", "source": "request"},
            )
        logger.info(
            "retention.erase tenant=%s %s=%s already=%s",
            tenant_id,
            resource_type,
            resource_id,
            result.already_erased,
        )
        return result

    # ---- sweep ------------------------------------------------------------

    def sweep(
        self, *, tenant_id: uuid.UUID, now: datetime | None = None
    ) -> SweepReport:
        """Erase expired, non-held resources for one tenant.

        For each erasable resource type with a finite ``ttl_days`` policy,
        finds resources whose reference timestamp is older than ``now -
        ttl_days``, are not under legal hold, and are not already erased, and
        erases them. Resources without a policy, or with ``ttl_days IS NULL``,
        are retained indefinitely (never touched).

        ``now`` is injectable for tests; defaults to the current UTC time.
        """
        now = now or datetime.now(UTC)
        report = SweepReport()
        for resource_type in (RESOURCE_RUN, RESOURCE_STORED_OBJECT):
            policy = self.get_policy(tenant_id, resource_type)
            if policy is None or policy.ttl_days is None:
                continue
            cutoff = now - timedelta(days=policy.ttl_days)
            candidate_ids = self._expired_candidates(
                tenant_id=tenant_id, resource_type=resource_type, cutoff=cutoff
            )
            for resource_id in candidate_ids:
                report.scanned += 1
                result = self._erase_one(
                    tenant_id=tenant_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    skip_legal_hold=True,
                )
                if result is None:
                    # Raced away (deleted) between listing and erase, or a hold
                    # was set in the gap — count it as a skip, not an erase.
                    report.skipped_legal_hold += 1
                    continue
                if result.already_erased:
                    report.already_erased += 1
                    continue
                report._erased(resource_type)
                self._audit.record(
                    action="retention.erased",
                    tenant_id=tenant_id,
                    target_kind=resource_type,
                    target_id=str(resource_id),
                    payload={"reason": "retention_sweep", "ttl_days": policy.ttl_days},
                )
        if report.erased or report.skipped_legal_hold:
            logger.info(
                "retention.sweep tenant=%s scanned=%d erased=%d held=%d already=%d",
                tenant_id,
                report.scanned,
                report.erased,
                report.skipped_legal_hold,
                report.already_erased,
            )
        return report

    def sweep_all_tenants(self, *, now: datetime | None = None) -> dict[uuid.UUID, SweepReport]:
        """Run :meth:`sweep` for every tenant that has at least one policy.

        Used by the periodic lifespan task. Only tenants with a retention policy
        are visited, so the cost scales with configured policies, not tenants.
        """
        with self._sf.session() as s:
            tenant_ids = list(
                s.scalars(select(RetentionPolicy.tenant_id).distinct())
            )
        out: dict[uuid.UUID, SweepReport] = {}
        for tid in tenant_ids:
            try:
                out[tid] = self.sweep(tenant_id=tid, now=now)
            except Exception:  # pragma: no cover - one tenant must not break the rest
                logger.exception("retention.sweep failed for tenant=%s", tid)
        return out

    # ---- internals --------------------------------------------------------

    @staticmethod
    def _require_erasable(resource_type: str) -> None:
        if resource_type not in ERASABLE_RESOURCE_TYPES:
            raise UnknownResourceError(
                f"resource_type {resource_type!r} is not erasable; "
                f"expected one of {sorted(ERASABLE_RESOURCE_TYPES)}"
            )

    @staticmethod
    def _get_policy(
        s: Session, tenant_id: uuid.UUID, resource_type: str
    ) -> RetentionPolicy | None:
        return s.scalar(
            select(RetentionPolicy).where(
                RetentionPolicy.tenant_id == tenant_id,
                RetentionPolicy.resource_type == resource_type,
            )
        )

    def _expired_candidates(
        self, *, tenant_id: uuid.UUID, resource_type: str, cutoff: datetime
    ) -> list[uuid.UUID]:
        """Ids of non-held, non-erased resources older than ``cutoff``.

        The ``legal_hold`` filter is applied here AND re-checked under the write
        transaction in :meth:`_erase_one`, so a hold set after listing still
        wins (TOCTOU-safe).
        """
        with self._sf.session() as s:
            if resource_type == RESOURCE_RUN:
                # Reference time: when the run ended, else when it started. A
                # never-ended run uses started_at so a wedged run can still age
                # out rather than pinning PII forever.
                ref = Run.ended_at.is_not(None)
                stmt = (
                    select(Run.id)
                    .where(
                        Run.tenant_id == tenant_id,
                        Run.legal_hold.is_(False),
                        Run.erased_at.is_(None),
                    )
                    .where(
                        # ended_at < cutoff when set, else started_at < cutoff
                        ((ref) & (Run.ended_at < cutoff))
                        | ((Run.ended_at.is_(None)) & (Run.started_at < cutoff))
                    )
                )
                return list(s.scalars(stmt))
            # stored_object
            stmt = (
                select(StoredObject.id)
                .where(
                    StoredObject.tenant_id == tenant_id,
                    StoredObject.legal_hold.is_(False),
                    StoredObject.status != StoredObjectStatus.ERASED,
                    StoredObject.erased_at.is_(None),
                    StoredObject.created_at < cutoff,
                )
            )
            return list(s.scalars(stmt))

    def _erase_one(
        self,
        *,
        tenant_id: uuid.UUID,
        resource_type: str,
        resource_id: uuid.UUID,
        skip_legal_hold: bool = False,
    ) -> EraseResult | None:
        """Erase a single resource in one short transaction.

        Returns the result, or ``None`` if the resource is absent/cross-tenant.
        When ``skip_legal_hold`` is True (the sweep path) a held resource yields
        ``None`` instead of raising — the caller counts it as a skip. When False
        (the explicit request path) a held resource raises
        :class:`LegalHoldError`.

        Object-store bytes are scrubbed before the row is flipped; a missing
        object is tolerated so a partially-completed prior erasure (row not yet
        flipped) is idempotent.
        """
        # Object-store delete first (outside is fine — it's idempotent), then the
        # DB flip in a short txn. We re-load + re-check hold inside the txn so the
        # listing-vs-erase gap can't erase a freshly-held resource.
        if resource_type == RESOURCE_STORED_OBJECT:
            return self._erase_stored_object(
                tenant_id=tenant_id,
                resource_id=resource_id,
                skip_legal_hold=skip_legal_hold,
            )
        return self._erase_run(
            tenant_id=tenant_id,
            resource_id=resource_id,
            skip_legal_hold=skip_legal_hold,
        )

    def _erase_run(
        self, *, tenant_id: uuid.UUID, resource_id: uuid.UUID, skip_legal_hold: bool
    ) -> EraseResult | None:
        now = datetime.now(UTC)
        with self._sf.session() as s:
            run = s.get(Run, resource_id)
            if run is None or run.tenant_id != tenant_id:
                return None
            if run.legal_hold:
                if skip_legal_hold:
                    return None
                raise LegalHoldError(
                    f"run {resource_id} is under legal hold; clear it before erasing"
                )
            if run.erased_at is not None:
                return EraseResult(
                    resource_type=RESOURCE_RUN,
                    resource_id=resource_id,
                    erased_at=run.erased_at,
                    already_erased=True,
                )
            # Scrub PII-bearing JSON. Leave a tiny marker so the columns read as
            # "erased" rather than legitimately-empty. Status/timeline metadata
            # (status, started/ended, workflow ref) is retained for audit.
            run.inputs = dict(_SCRUBBED)
            run.outputs = dict(_SCRUBBED)
            run.error = None
            run.checkpoint = None
            run.erased_at = now
            # Scrub the denormalized event payloads too — they mirror node I/O.
            events = list(
                s.scalars(select(RunEvent).where(RunEvent.run_id == resource_id))
            )
            for ev in events:
                ev.payload = dict(_SCRUBBED)
            s.commit()
        return EraseResult(
            resource_type=RESOURCE_RUN, resource_id=resource_id, erased_at=now
        )

    def _erase_stored_object(
        self, *, tenant_id: uuid.UUID, resource_id: uuid.UUID, skip_legal_hold: bool
    ) -> EraseResult | None:
        now = datetime.now(UTC)
        # Read the URI + hold state first so we can delete bytes outside the
        # write txn; then re-check hold + erased state under the txn.
        with self._sf.session() as s:
            obj = s.get(StoredObject, resource_id)
            if obj is None or obj.tenant_id != tenant_id:
                return None
            if obj.legal_hold:
                if skip_legal_hold:
                    return None
                raise LegalHoldError(
                    f"stored_object {resource_id} is under legal hold; "
                    "clear it before erasing"
                )
            if obj.status == StoredObjectStatus.ERASED or obj.erased_at is not None:
                return EraseResult(
                    resource_type=RESOURCE_STORED_OBJECT,
                    resource_id=resource_id,
                    erased_at=obj.erased_at or now,
                    already_erased=True,
                )
            uri = obj.uri
        # Delete the bytes. A missing object is fine (idempotent re-erasure or a
        # row whose bytes were already gone) — any other store error propagates
        # so we don't tombstone a row whose bytes we failed to remove.
        try:
            self._object_store.delete(uri)
        except (ObjectNotFound, FileNotFoundError):
            logger.info("retention.erase stored_object bytes already gone uri=%s", uri)
        # Flip the row to a tombstone under a short txn, re-checking the hold.
        with self._sf.session() as s:
            obj = s.get(StoredObject, resource_id)
            if obj is None or obj.tenant_id != tenant_id:
                return None
            if obj.legal_hold and not skip_legal_hold:
                raise LegalHoldError(
                    f"stored_object {resource_id} is under legal hold; "
                    "clear it before erasing"
                )
            if obj.legal_hold:
                return None
            if obj.status == StoredObjectStatus.ERASED:
                return EraseResult(
                    resource_type=RESOURCE_STORED_OBJECT,
                    resource_id=resource_id,
                    erased_at=obj.erased_at or now,
                    already_erased=True,
                )
            obj.status = StoredObjectStatus.ERASED
            obj.erased_at = now
            s.commit()
        return EraseResult(
            resource_type=RESOURCE_STORED_OBJECT,
            resource_id=resource_id,
            erased_at=now,
        )

    def _load_row(
        self,
        s: Session,
        resource_type: str,
        tenant_id: uuid.UUID,
        resource_id: uuid.UUID,
    ) -> Run | StoredObject | None:
        if resource_type == RESOURCE_RUN:
            run = s.get(Run, resource_id)
            return run if run is not None and run.tenant_id == tenant_id else None
        obj = s.get(StoredObject, resource_id)
        return obj if obj is not None and obj.tenant_id == tenant_id else None
