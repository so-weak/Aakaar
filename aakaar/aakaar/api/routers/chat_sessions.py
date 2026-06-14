"""Chat session endpoints — stateful conversational planning.

The legacy stateless `/chat` endpoint stays alive for tools that just
want one round-trip; sessions add:

  - persistent chat history → planner sees prior turns and the current
    draft DAG, so refinements like "use selector X instead" actually
    refine instead of starting from scratch
  - draft tracking → the session carries the latest dag + rationale; the
    UI mirrors them
  - dirty detection + confirm-on-save/update → first save creates a
    workflow; subsequent edits surface as dirty drift, requiring an
    explicit `confirm: true` to PATCH the saved version
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from aakaar.api.deps import (
    get_agentic_planner,
    get_planner,
    get_session,
    require_tenant_user,
)
from aakaar.api.repositories import chat_sessions as sessions_repo
from aakaar.api.repositories import grants as grants_repo
from aakaar.api.repositories import workflows as workflows_repo
from aakaar.api.schemas import (
    ChatMessageResponse,
    ChatSaveRequest,
    ChatSendRequest,
    ChatSessionCreateRequest,
    ChatSessionResponse,
    ChatSessionSummaryResponse,
    WorkflowResponse,
)
from aakaar.db.models import ChatMessage, ChatSession, User, Workflow
from aakaar.planner import PlannerError, PlannerService
from aakaar.planner.llm import LLMMessage, Role
from aakaar.shared.dag.types import Dag
from aakaar.shared.planner.responses import ClarifyResponse, DagResponse, MissingResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat/sessions", tags=["chat-sessions"])


# ---------- helpers -----------------------------------------------------------


def _saved_dag(
    db: Session, *, tenant_id: uuid.UUID, sess: ChatSession
) -> dict[str, Any] | None:
    """The workflow version this session is currently bound to, as a dict.
    Used for dirty comparison and to seed the planner's `current_dag`
    context after the first save."""
    if sess.workflow_id is None or sess.saved_version is None:
        return None
    wfv = workflows_repo.get_version(
        db, tenant_id, sess.workflow_id, sess.saved_version
    )
    return wfv.dag if wfv else None


def _planner_history(messages: list[ChatMessage]) -> list[LLMMessage]:
    """Materialize the session's prior turns as LLMMessage so the planner
    can see them. We keep planner replies in the conversation as
    assistant role with their rationale text — it's enough context for
    the model to recognize what it last proposed without re-shipping the
    full DAG (which is already passed via `current_dag`)."""
    out: list[LLMMessage] = []
    for m in messages:
        if m.role == "user":
            out.append(LLMMessage(role=Role.USER, content=m.text))
        elif m.role == "planner":
            text = m.text or m.payload.get("rationale", "")
            if text:
                out.append(LLMMessage(role=Role.ASSISTANT, content=text))
    return out


def _serialize_message(m: ChatMessage) -> ChatMessageResponse:
    return ChatMessageResponse(
        id=m.id,
        sequence=m.sequence,
        role=m.role,
        text=m.text,
        payload=m.payload or {},
        at=m.at,
    )


def _serialize_session(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    sess: ChatSession,
    messages: list[ChatMessage] | None = None,
) -> ChatSessionResponse:
    if messages is None:
        messages = sessions_repo.list_messages(db, session_id=sess.id)
    saved = _saved_dag(db, tenant_id=tenant_id, sess=sess)
    is_dirty = sessions_repo.compute_dirty(sess, saved)
    draft = Dag.model_validate(sess.draft_dag) if sess.draft_dag else None
    return ChatSessionResponse(
        id=sess.id,
        tenant_id=sess.tenant_id,
        user_id=sess.user_id,
        title=sess.title,
        workflow_id=sess.workflow_id,
        saved_version=sess.saved_version,
        draft_dag=draft,
        draft_rationale=sess.draft_rationale,
        is_dirty=is_dirty,
        created_at=sess.created_at,
        updated_at=sess.updated_at,
        messages=[_serialize_message(m) for m in messages],
    )


def _serialize_summary(
    db: Session, *, tenant_id: uuid.UUID, sess: ChatSession
) -> ChatSessionSummaryResponse:
    saved = _saved_dag(db, tenant_id=tenant_id, sess=sess)
    return ChatSessionSummaryResponse(
        id=sess.id,
        title=sess.title,
        workflow_id=sess.workflow_id,
        saved_version=sess.saved_version,
        is_dirty=sessions_repo.compute_dirty(sess, saved),
        created_at=sess.created_at,
        updated_at=sess.updated_at,
    )


# ---------- create / list / get / delete --------------------------------------


@router.post("", response_model=ChatSessionResponse, status_code=status.HTTP_201_CREATED)
def create_session(
    body: ChatSessionCreateRequest,
    user: Annotated[User, Depends(require_tenant_user)],
    db: Annotated[Session, Depends(get_session)],
) -> ChatSessionResponse:
    assert user.tenant_id is not None
    sess = sessions_repo.create_session(
        db, tenant_id=user.tenant_id, user_id=user.id, title=body.title
    )
    db.commit()
    return _serialize_session(db, tenant_id=user.tenant_id, sess=sess, messages=[])


@router.get("", response_model=list[ChatSessionSummaryResponse])
def list_sessions(
    user: Annotated[User, Depends(require_tenant_user)],
    db: Annotated[Session, Depends(get_session)],
) -> list[ChatSessionSummaryResponse]:
    assert user.tenant_id is not None
    rows = sessions_repo.list_sessions_for_user(
        db, tenant_id=user.tenant_id, user_id=user.id
    )
    return [_serialize_summary(db, tenant_id=user.tenant_id, sess=s) for s in rows]


@router.get("/{session_id}", response_model=ChatSessionResponse)
def get_session_detail(
    session_id: uuid.UUID,
    user: Annotated[User, Depends(require_tenant_user)],
    db: Annotated[Session, Depends(get_session)],
) -> ChatSessionResponse:
    assert user.tenant_id is not None
    sess = sessions_repo.get_session(
        db, tenant_id=user.tenant_id, session_id=session_id
    )
    if sess is None or sess.user_id != user.id:
        raise HTTPException(status_code=404, detail="session not found")
    return _serialize_session(db, tenant_id=user.tenant_id, sess=sess)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(
    session_id: uuid.UUID,
    user: Annotated[User, Depends(require_tenant_user)],
    db: Annotated[Session, Depends(get_session)],
) -> None:
    assert user.tenant_id is not None
    sess = sessions_repo.get_session(
        db, tenant_id=user.tenant_id, session_id=session_id
    )
    if sess is None or sess.user_id != user.id:
        raise HTTPException(status_code=404, detail="session not found")
    db.delete(sess)
    db.commit()


# ---------- send a message ----------------------------------------------------


@router.post("/{session_id}/messages", response_model=ChatSessionResponse)
async def send_message(
    session_id: uuid.UUID,
    body: ChatSendRequest,
    user: Annotated[User, Depends(require_tenant_user)],
    db: Annotated[Session, Depends(get_session)],
    planner: Annotated[PlannerService, Depends(get_planner)],
    agentic: Annotated[Any, Depends(get_agentic_planner)] = None,
) -> ChatSessionResponse:
    """Append the user's turn, run the planner with full prior history +
    current draft DAG as context, persist the planner reply, return the
    refreshed session.

    Auto-fallback: if the one-shot planner returns a clarify response AND
    a Playwright-backed agentic planner is wired, retry agentically.
    The agentic loop can `navigate` / `inspect_page` / `login_with_grant`
    inside a planning browser session and may produce a concrete DAG
    where the one-shot planner only had questions.
    """
    assert user.tenant_id is not None
    sess = sessions_repo.get_session(
        db, tenant_id=user.tenant_id, session_id=session_id
    )
    if sess is None or sess.user_id != user.id:
        raise HTTPException(status_code=404, detail="session not found")

    prior = sessions_repo.list_messages(db, session_id=sess.id)
    history = _planner_history(prior)
    logger.info(
        "chat session message session_id=%s user_id=%s prior_msgs=%d msg_len=%d",
        session_id,
        user.id,
        len(prior),
        len(body.message or ""),
    )

    # Persist the user turn before calling the planner so a planner failure
    # doesn't lose what the user just typed.
    sessions_repo.append_user_message(db, session_id=sess.id, text=body.message)
    db.commit()

    granted = grants_repo.list_granted_refs(db, user.tenant_id)
    grant_map = _grant_map_for_tenant(db, user.tenant_id)
    granted_aliases = {ref: sorted(am.keys()) for ref, am in grant_map.items()}
    grant_input_defaults = {
        ref: {alias: dict(info.get("input_defaults") or {}) for alias, info in am.items()}
        for ref, am in grant_map.items()
    }
    current_dag = Dag.model_validate(sess.draft_dag) if sess.draft_dag else None

    try:
        resp = planner.plan(
            user_message=body.message,
            granted_capabilities=granted,
            granted_aliases=granted_aliases,
            grant_input_defaults=grant_input_defaults,
            current_dag=current_dag,
            chat_history=history,
        )
    except PlannerError as e:
        logger.exception("planner failed in session_id=%s", session_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"planner failed: {e}",
        ) from e

    # Auto-escalate: a clarify reply suggests the planner needed page
    # context it couldn't get. Retry with the agentic loop if it's wired.
    if isinstance(resp, ClarifyResponse) and agentic is not None:
        try:
            agentic_resp = await agentic.plan(
                user_message=body.message,
                tenant_id=user.tenant_id,
                granted_capabilities=granted,
                granted_capability_grants=grant_map,
                current_dag=current_dag,
                chat_history=history,
            )
            # Prefer the agentic outcome if it's a DAG/missing; if it
            # also returned clarify, keep the original questions (the
            # one-shot planner's questions are usually crisper than the
            # "I explored and couldn't finalize" fallback).
            if not isinstance(agentic_resp, ClarifyResponse):
                resp = agentic_resp
        except Exception:  # noqa: BLE001
            # Agentic failure must not break a request that already had a
            # valid one-shot answer. Stick with the clarify and surface a
            # short note in observability via the standard logger.
            logger.exception(
                "agentic-planner fallback failed; keeping one-shot clarify"
            )

    payload = _planner_response_payload(resp)
    sessions_repo.append_planner_message(db, session_id=sess.id, payload=payload)
    sessions_repo.update_draft_from_response(db, session=sess, response=payload)
    db.commit()
    return _serialize_session(db, tenant_id=user.tenant_id, sess=sess)


def _grant_map_for_tenant(
    db: Session, tenant_id: uuid.UUID
) -> dict[str, dict[str, dict[str, Any]]]:
    """Build the (capability_ref → alias → {vault_ref, input_defaults})
    map the agentic runner needs to look up creds at plan time."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for g in grants_repo.list_grants(db, tenant_id):
        if not g.enabled:
            continue
        out.setdefault(g.capability_ref, {})[g.account_alias] = {
            "vault_ref": g.vault_ref,
            "input_defaults": dict(g.input_defaults or {}),
        }
    return out


def _planner_response_payload(resp: object) -> dict[str, Any]:
    """Flatten the discriminated PlannerResponse to the same JSON shape the
    legacy /chat endpoint returns. The frontend already understands it."""
    if isinstance(resp, DagResponse):
        return {
            "kind": "dag",
            "rationale": resp.rationale,
            "dag": resp.dag.model_dump(by_alias=True),
            "questions": [],
            "needed": [],
            "explanation": "",
        }
    if isinstance(resp, ClarifyResponse):
        return {
            "kind": "clarify",
            "rationale": "",
            "dag": None,
            "questions": list(resp.questions),
            "needed": [],
            "explanation": "",
        }
    assert isinstance(resp, MissingResponse)
    return {
        "kind": "missing",
        "rationale": "",
        "dag": None,
        "questions": [],
        "needed": list(resp.needed),
        "explanation": resp.explanation,
    }


# ---------- save (create or update workflow) ---------------------------------


@router.post("/{session_id}/save", response_model=WorkflowResponse)
def save_session(
    session_id: uuid.UUID,
    body: ChatSaveRequest,
    user: Annotated[User, Depends(require_tenant_user)],
    db: Annotated[Session, Depends(get_session)],
) -> WorkflowResponse:
    """Persist the session's draft as a workflow.

    First save (no `workflow_id` on the session): requires `name`. Creates
    a new workflow and binds the session to it.

    Subsequent saves: requires `confirm=true`. Adds a new version iff the
    draft differs from the bound `saved_version`. If the draft matches
    (no drift), returns the existing workflow without writing.
    """
    assert user.tenant_id is not None
    sess = sessions_repo.get_session(
        db, tenant_id=user.tenant_id, session_id=session_id
    )
    if sess is None or sess.user_id != user.id:
        raise HTTPException(status_code=404, detail="session not found")
    if sess.draft_dag is None:
        raise HTTPException(status_code=400, detail="session has no draft DAG to save")

    draft = Dag.model_validate(sess.draft_dag)

    if sess.workflow_id is None:
        # First save: requires a name; creates a workflow.
        if not body.name or not body.name.strip():
            raise HTTPException(
                status_code=400,
                detail="`name` is required for the first save of a session",
            )
        workflow, version = workflows_repo.create_workflow(
            db,
            tenant_id=user.tenant_id,
            created_by=user.id,
            name=body.name.strip(),
            description=body.description,
            dag=draft,
            rationale=sess.draft_rationale,
        )
        sess.workflow_id = workflow.id
        sess.saved_version = version.version
        if sess.title == "Untitled session":
            sess.title = workflow.name
        db.commit()
        return _to_workflow_response(workflow)

    # Update path: must confirm, must actually be dirty.
    saved = _saved_dag(db, tenant_id=user.tenant_id, sess=sess)
    if not sessions_repo.compute_dirty(sess, saved):
        # Nothing changed; return the existing workflow.
        existing = workflows_repo.get_workflow(db, user.tenant_id, sess.workflow_id)
        if existing is None:
            raise HTTPException(status_code=410, detail="bound workflow has been deleted")
        return _to_workflow_response(existing)
    if not body.confirm:
        raise HTTPException(
            status_code=409,
            detail=(
                "session draft differs from the saved workflow; resubmit with "
                "`confirm: true` to write a new version"
            ),
        )

    try:
        version = workflows_repo.add_version(
            db,
            tenant_id=user.tenant_id,
            workflow_id=sess.workflow_id,
            created_by=user.id,
            dag=draft,
            rationale=sess.draft_rationale,
        )
    except workflows_repo.WorkflowNotFound as e:
        raise HTTPException(status_code=410, detail=f"workflow gone: {e}") from e
    sess.saved_version = version.version
    db.commit()
    updated = workflows_repo.get_workflow(db, user.tenant_id, sess.workflow_id)
    assert updated is not None
    return _to_workflow_response(updated)


def _to_workflow_response(workflow: Workflow) -> WorkflowResponse:
    return WorkflowResponse(
        id=workflow.id,
        tenant_id=workflow.tenant_id,
        created_by=workflow.created_by,
        name=workflow.name,
        description=workflow.description,
        latest_version=workflow.latest_version,
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
    )
