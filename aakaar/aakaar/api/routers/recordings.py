"""Activity recording REST API (tenant-admin only).

Start/poll/stop/discard a desktop capture running on one of the tenant's
agents (the agent-side `cap.activity_recording` capability, reached over the
existing remote dispatch path). Stopping compiles the privacy-reduced event
stream into a draft workflow that is saved through the normal workflows
repository so it appears in the UI for review.

Privacy: raw keystrokes never leave the agent. The server validates the
returned stream against that contract (rejecting anything that looks like raw
input), holds events in memory only for the duration of the stop call, and
persists nothing but the compiled draft — typed text appears only as
<REPLACE_REDACTED_TEXT_n> placeholders.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from aakaar.api.deps import get_audit, get_session, require_tenant_admin
from aakaar.api.repositories import workflows as workflows_repo
from aakaar.db.models import User
from aakaar.services.audit import AuditRecorder
from aakaar.services.recordings import (
    AgentUnavailable,
    EmptyRecording,
    EventContractViolation,
    RecordingEntry,
    RecordingError,
    RecordingLimitReached,
    RecordingNotFound,
    RecordingService,
    RecordingUnavailable,
    compile_recording,
)
from aakaar.shared.dag.types import Dag

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/recordings", tags=["recordings"])

PRIVACY_NOTE = (
    "Keystrokes are redacted on the agent: only allowlisted navigation hotkeys "
    "(enter, tab, esc, ctrl+a/c/v/s, ctrl+tab, alt+tab, shift+tab) are captured "
    "verbatim; all other typing is recorded as character counts only. Captured "
    "events are held in memory and never written to disk; the compiled draft "
    "contains placeholders instead of typed text."
)


def get_recordings(request: Request) -> RecordingService:
    svc = getattr(request.app.state, "recordings", None)
    if not isinstance(svc, RecordingService):
        raise RuntimeError("RecordingService not attached to app.state.recordings")
    return svc


def _http_error(e: RecordingError) -> HTTPException:
    if isinstance(e, RecordingNotFound):
        return HTTPException(status_code=404, detail="recording not found")
    if isinstance(e, RecordingLimitReached | AgentUnavailable):
        return HTTPException(status_code=409, detail=str(e))
    if isinstance(e, RecordingUnavailable):
        return HTTPException(status_code=503, detail=str(e))
    # AgentRecordingError and anything else agent-shaped: upstream fault.
    return HTTPException(status_code=502, detail=str(e))


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------- schemas (recording-local by design; not part of api/schemas.py) --


class RecordingStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    agent_alias: Annotated[
        str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_\-]{0,63}$")
    ]
    max_events: int = Field(default=2000, ge=1, le=5000)


class RecordingStartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recording_id: str
    status: Literal["recording"] = "recording"
    name: str
    agent_alias: str
    event_count: int = 0
    started_at: datetime
    expires_at: datetime
    privacy_note: str = PRIVACY_NOTE


class RecordingStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recording_id: str
    status: str
    name: str
    agent_alias: str
    event_count: int
    truncated: bool = False
    """The agent auto-stopped at its event cap; later actions aren't captured."""
    duration_seconds: float
    started_at: datetime
    expires_at: datetime


class RecordingListItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recording_id: str
    status: Literal["recording"] = "recording"
    name: str
    agent_alias: str
    started_at: datetime
    expires_at: datetime


class RecordingStopResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recording_id: str
    status: Literal["stopped"] = "stopped"
    event_count: int
    workflow_id: uuid.UUID
    workflow_name: str
    draft_dag: Dag
    warnings: list[str]
    rationale: str


def _start_response(entry: RecordingEntry) -> RecordingStartResponse:
    return RecordingStartResponse(
        recording_id=entry.recording_id,
        name=entry.name,
        agent_alias=entry.agent_alias,
        started_at=entry.started_at,
        expires_at=entry.expires_at,
    )


# ---------- endpoints --------------------------------------------------------


@router.post("", response_model=RecordingStartResponse, status_code=status.HTTP_201_CREATED)
async def start_recording(
    body: RecordingStartRequest,
    admin: Annotated[User, Depends(require_tenant_admin)],
    recordings: Annotated[RecordingService, Depends(get_recordings)],
    audit: Annotated[AuditRecorder, Depends(get_audit)],
) -> RecordingStartResponse:
    assert admin.tenant_id is not None
    try:
        entry = await recordings.begin_recording(
            tenant_id=admin.tenant_id,
            created_by=admin.id,
            name=body.name.strip(),
            agent_alias=body.agent_alias,
            max_events=body.max_events,
        )
    except RecordingError as e:
        raise _http_error(e) from e
    audit.record(
        action="recording.start",
        tenant_id=admin.tenant_id,
        actor_id=admin.id,
        target_kind="recording",
        target_id=entry.recording_id,
        payload={"agent": entry.agent_alias, "name": entry.name, "max_events": body.max_events},
    )
    return _start_response(entry)


# async (despite doing only in-memory work) so list_active's opportunistic
# discard drain runs on the event-loop thread. A sync endpoint runs in a
# threadpool worker with no running loop, where _kick_drain silently no-ops and
# an entry that expires on this path waits for the next 60s sweep to discard.
@router.get("", response_model=list[RecordingListItem])
async def list_recordings(
    admin: Annotated[User, Depends(require_tenant_admin)],
    recordings: Annotated[RecordingService, Depends(get_recordings)],
) -> list[RecordingListItem]:
    assert admin.tenant_id is not None
    return [
        RecordingListItem(
            recording_id=e.recording_id,
            name=e.name,
            agent_alias=e.agent_alias,
            started_at=e.started_at,
            expires_at=e.expires_at,
        )
        for e in recordings.list_active(admin.tenant_id)
    ]


@router.get("/{recording_id}", response_model=RecordingStatusResponse)
async def recording_status(
    recording_id: str,
    admin: Annotated[User, Depends(require_tenant_admin)],
    recordings: Annotated[RecordingService, Depends(get_recordings)],
) -> RecordingStatusResponse:
    assert admin.tenant_id is not None
    try:
        entry, agent_view = await recordings.recording_status(
            tenant_id=admin.tenant_id, recording_id=recording_id
        )
    except RecordingError as e:
        raise _http_error(e) from e
    return RecordingStatusResponse(
        recording_id=entry.recording_id,
        # The status string is agent-supplied; cap it so a misbehaving agent
        # can't bloat responses.
        status=str(agent_view.get("status", "recording"))[:32],
        name=entry.name,
        agent_alias=entry.agent_alias,
        event_count=_safe_int(agent_view.get("event_count")),
        truncated=bool(agent_view.get("truncated")),
        duration_seconds=(datetime.now(UTC) - entry.started_at).total_seconds(),
        started_at=entry.started_at,
        expires_at=entry.expires_at,
    )


@router.post("/{recording_id}/stop", response_model=RecordingStopResponse)
async def stop_recording(
    recording_id: str,
    admin: Annotated[User, Depends(require_tenant_admin)],
    recordings: Annotated[RecordingService, Depends(get_recordings)],
    session: Annotated[Session, Depends(get_session)],
    audit: Annotated[AuditRecorder, Depends(get_audit)],
) -> RecordingStopResponse:
    assert admin.tenant_id is not None
    try:
        entry, events, truncated = await recordings.stop_recording(
            tenant_id=admin.tenant_id, recording_id=recording_id
        )
    except EventContractViolation as e:
        raise HTTPException(
            status_code=502,
            detail=f"agent violated the recording privacy contract: {e}",
        ) from e
    except RecordingError as e:
        raise _http_error(e) from e

    try:
        compiled = compile_recording(events, agent_alias=entry.agent_alias)
    except EmptyRecording as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    warnings = list(compiled.warnings)
    if truncated:
        # The agent hit its event cap and auto-stopped, so the user's final
        # actions were never captured — the draft is incomplete by definition.
        warnings.append(
            "The capture hit its event limit and auto-stopped; actions after "
            "that point were not recorded, so the draft is incomplete."
        )

    workflow, _ = workflows_repo.create_workflow(
        session,
        tenant_id=admin.tenant_id,
        created_by=admin.id,
        name=entry.name,
        description=(
            f"Draft workflow compiled from an activity recording on agent "
            f"'{entry.agent_alias}'."
        ),
        dag=compiled.dag,
        rationale=compiled.rationale,
    )
    session.commit()
    logger.info(
        "recording compiled id=%s workflow=%s nodes=%d warnings=%d truncated=%s",
        entry.recording_id,
        workflow.id,
        len(compiled.dag.nodes),
        len(warnings),
        truncated,
    )
    audit.record(
        action="recording.stop",
        tenant_id=admin.tenant_id,
        actor_id=admin.id,
        target_kind="recording",
        target_id=entry.recording_id,
        payload={
            "agent": entry.agent_alias,
            "events": len(events),
            "nodes": len(compiled.dag.nodes),
            "workflow_id": str(workflow.id),
            "truncated": truncated,
        },
    )
    return RecordingStopResponse(
        recording_id=entry.recording_id,
        event_count=len(events),
        workflow_id=workflow.id,
        workflow_name=entry.name,
        draft_dag=compiled.dag,
        warnings=warnings,
        rationale=compiled.rationale,
    )


@router.delete("/{recording_id}", status_code=status.HTTP_204_NO_CONTENT)
async def discard_recording(
    recording_id: str,
    admin: Annotated[User, Depends(require_tenant_admin)],
    recordings: Annotated[RecordingService, Depends(get_recordings)],
    audit: Annotated[AuditRecorder, Depends(get_audit)],
) -> None:
    assert admin.tenant_id is not None
    try:
        entry = await recordings.discard_recording(
            tenant_id=admin.tenant_id, recording_id=recording_id
        )
    except RecordingError as e:
        raise _http_error(e) from e
    audit.record(
        action="recording.discard",
        tenant_id=admin.tenant_id,
        actor_id=admin.id,
        target_kind="recording",
        target_id=entry.recording_id,
        payload={"agent": entry.agent_alias, "name": entry.name},
    )
