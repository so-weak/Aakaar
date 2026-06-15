"""Audit log read + tamper-evidence API.

Tenant-admins see and verify their own tenant's audit trail; superusers can
verify or export any tenant. The audit log is a per-tenant hash chain (see
:mod:`aakaar.services.audit.chain`): ``/audit/verify`` recomputes it to prove
non-tampering, and ``/audit/export`` streams it in chain order — including
``seq`` and the ``prev_hash``/``entry_hash`` links — so an external auditor can
re-verify it independently.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from aakaar.api.deps import (
    AppDependencies,
    get_deps,
    get_session,
    require_superuser,
    require_tenant_admin,
)
from aakaar.api.repositories import audit as audit_repo
from aakaar.api.repositories import tenants as tenants_repo
from aakaar.api.schemas import AuditEntry, AuditListResponse, AuditVerifyResponse
from aakaar.db.models import User
from aakaar.services.audit.chain import GENESIS_PREV
from aakaar.services.audit.ledger import iter_chain, verify_chain

router = APIRouter(prefix="/audit", tags=["audit"])

# Page size for streaming export: each batch is fetched in its own short
# session so a large ledger is never fully materialized. The chain is read
# strictly in ``seq`` order, resuming after the last seq of the prior page.
_EXPORT_PAGE = 500


@router.get("", response_model=AuditListResponse)
def list_audit(
    user: Annotated[User, Depends(require_tenant_admin)],
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    action_prefix: Annotated[str | None, Query(max_length=64)] = None,
) -> AuditListResponse:
    assert user.tenant_id is not None
    rows = audit_repo.list_for_tenant(
        session,
        tenant_id=user.tenant_id,
        limit=limit,
        offset=offset,
        action_prefix=action_prefix,
    )
    total = audit_repo.count_for_tenant(
        session, tenant_id=user.tenant_id, action_prefix=action_prefix
    )
    return AuditListResponse(
        entries=[AuditEntry.model_validate(r) for r in rows], total=total
    )


def _verify_response(session: Session, tenant_id: uuid.UUID) -> AuditVerifyResponse:
    result = verify_chain(session, tenant_id=tenant_id)
    return AuditVerifyResponse(
        ok=result.intact,
        entries_checked=result.count,
        first_seq=result.first_seq,
        last_seq=result.last_seq,
        first_broken_seq=result.broken_at,
        reason=result.reason,
    )


@router.get("/verify", response_model=AuditVerifyResponse)
def verify_audit(
    user: Annotated[User, Depends(require_tenant_admin)],
    session: Annotated[Session, Depends(get_session)],
) -> AuditVerifyResponse:
    """Recompute the calling tenant's audit hash chain end-to-end."""
    assert user.tenant_id is not None
    return _verify_response(session, user.tenant_id)


@router.get("/tenants/{tenant_id}/verify", response_model=AuditVerifyResponse)
def verify_tenant_audit(
    tenant_id: uuid.UUID,
    _: Annotated[User, Depends(require_superuser)],
    session: Annotated[Session, Depends(get_session)],
) -> AuditVerifyResponse:
    """Superuser: verify any tenant's audit chain (cross-tenant)."""
    if tenants_repo.get_tenant(session, tenant_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")
    return _verify_response(session, tenant_id)


def _export_lines(deps: AppDependencies, tenant_id: uuid.UUID) -> Iterator[str]:
    """Yield the tenant's chained audit rows as JSONL, in ``seq`` order.

    Streamed page-by-page, each page in its own short session, so the response
    never holds a session open across the whole (potentially huge) ledger and
    never loads it all into memory. Each line carries ``seq`` + the chain
    hashes so an auditor can independently recompute and re-verify offline.
    The genesis row's NULL ``prev_hash`` is emitted as the explicit
    :data:`GENESIS_PREV` sentinel the verifier substitutes, so re-hashing the
    exported bytes reproduces the stored ``entry_hash`` without special-casing.
    """
    after_seq: int | None = None
    while True:
        with deps.session_factory.session() as s:
            rows = list(
                iter_chain(
                    s, tenant_id=tenant_id, after_seq=after_seq, limit=_EXPORT_PAGE
                )
            )
            # Build line strings while the session is open (payload is loaded);
            # the session closes before we yield so we never stream across it.
            page = [
                json.dumps(
                    {
                        "seq": r.seq,
                        "tenant_id": str(tenant_id),
                        "actor_id": str(r.actor_id) if r.actor_id is not None else None,
                        "action": r.action,
                        "target_kind": r.target_kind,
                        "target_id": r.target_id,
                        "at": r.at.isoformat(),
                        "payload": r.payload,
                        "prev_hash": r.prev_hash if r.prev_hash is not None else GENESIS_PREV,
                        "entry_hash": r.entry_hash,
                    },
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
                for r in rows
            ]
        if not page:
            return
        yield from page
        if len(page) < _EXPORT_PAGE:
            return  # short page => last page; avoid an extra empty round-trip
        last_seq = rows[-1].seq
        assert last_seq is not None  # iter_chain only yields sequenced rows
        after_seq = last_seq


def _export_response(
    deps: AppDependencies, tenant_id: uuid.UUID
) -> StreamingResponse:
    filename = f"audit-{tenant_id}.jsonl"
    return StreamingResponse(
        _export_lines(deps, tenant_id),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export")
def export_audit(
    user: Annotated[User, Depends(require_tenant_admin)],
    deps: Annotated[AppDependencies, Depends(get_deps)],
) -> StreamingResponse:
    """Stream the calling tenant's audit chain as JSONL for external re-verify."""
    assert user.tenant_id is not None
    return _export_response(deps, user.tenant_id)


@router.get("/tenants/{tenant_id}/export")
def export_tenant_audit(
    tenant_id: uuid.UUID,
    _: Annotated[User, Depends(require_superuser)],
    deps: Annotated[AppDependencies, Depends(get_deps)],
    session: Annotated[Session, Depends(get_session)],
) -> StreamingResponse:
    """Superuser: stream any tenant's audit chain as JSONL (cross-tenant)."""
    if tenants_repo.get_tenant(session, tenant_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")
    return _export_response(deps, tenant_id)
