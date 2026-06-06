"""Chat session repository.

Two tables: `chat_sessions` (header + draft) and `chat_messages` (turns).
The repo owns sequence numbering, dirty-flag computation, and the rules
for binding a session to a workflow (first save creates, subsequent
saves PATCH, drift is detected from `saved_version`).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aakaar.db.models import ChatMessage, ChatSession

# ---------- create / list / get -----------------------------------------------


def create_session(
    session_db: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    title: str | None = None,
) -> ChatSession:
    s = ChatSession(
        tenant_id=tenant_id,
        user_id=user_id,
        title=title or "Untitled session",
    )
    session_db.add(s)
    session_db.flush()
    return s


def list_sessions_for_user(
    session_db: Session, *, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> list[ChatSession]:
    return list(
        session_db.scalars(
            select(ChatSession)
            .where(ChatSession.tenant_id == tenant_id, ChatSession.user_id == user_id)
            .order_by(ChatSession.updated_at.desc())
        )
    )


def get_session(
    session_db: Session, *, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> ChatSession | None:
    s = session_db.get(ChatSession, session_id)
    if s is None or s.tenant_id != tenant_id:
        return None
    return s


def list_messages(
    session_db: Session, *, session_id: uuid.UUID
) -> list[ChatMessage]:
    return list(
        session_db.scalars(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.sequence)
        )
    )


# ---------- append + update ---------------------------------------------------


def _next_sequence(session_db: Session, *, session_id: uuid.UUID) -> int:
    """Highest existing sequence + 1, starting at 0 for an empty session."""
    rows = session_db.scalars(
        select(ChatMessage.sequence)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.sequence.desc())
        .limit(1)
    ).all()
    return (rows[0] + 1) if rows else 0


def append_user_message(
    session_db: Session, *, session_id: uuid.UUID, text: str
) -> ChatMessage:
    msg = ChatMessage(
        session_id=session_id,
        sequence=_next_sequence(session_db, session_id=session_id),
        role="user",
        text=text,
        payload={},
    )
    session_db.add(msg)
    session_db.flush()
    return msg


def append_planner_message(
    session_db: Session,
    *,
    session_id: uuid.UUID,
    payload: dict[str, Any],
) -> ChatMessage:
    """Append the planner's structured response. The chat-bubble text is
    derived from the rationale/explanation/question summary so the UI has
    something readable even if it ignores `payload`."""
    rationale = payload.get("rationale") or ""
    if payload.get("kind") == "clarify":
        bubble = (rationale + "\n" if rationale else "") + "\n".join(
            f"• {q}" for q in payload.get("questions", [])
        )
    elif payload.get("kind") == "missing":
        bubble = payload.get("explanation") or rationale
    else:
        bubble = rationale or "Drafted a workflow."
    msg = ChatMessage(
        session_id=session_id,
        sequence=_next_sequence(session_db, session_id=session_id),
        role="planner",
        text=bubble[:8000],
        payload=payload,
    )
    session_db.add(msg)
    session_db.flush()
    return msg


def update_draft_from_response(
    session_db: Session,
    *,
    session: ChatSession,
    response: dict[str, Any],
) -> None:
    """Mirror a fresh planner response onto the session's draft fields.
    Only `kind=dag` updates the draft; clarify/missing leave it alone."""
    if response.get("kind") == "dag" and response.get("dag") is not None:
        session.draft_dag = response["dag"]
        session.draft_rationale = response.get("rationale") or ""
    session_db.flush()


# ---------- dirty detection ---------------------------------------------------


def compute_dirty(session: ChatSession, saved_dag: dict[str, Any] | None) -> bool:
    """True iff there's a draft that differs from what was last saved.

    - No draft yet → not dirty (nothing to save).
    - Draft but no saved workflow → dirty (first save needed).
    - Draft and saved version → dirty iff the JSON differs.

    DAGs are compared by canonical JSON to avoid false positives from key
    ordering. The id/version fields on the draft are zeroed before save,
    so we strip them on the saved side too before comparing.
    """
    if session.draft_dag is None:
        return False
    if saved_dag is None:
        return True
    return _normalize(session.draft_dag) != _normalize(saved_dag)


def _normalize(dag: dict[str, Any]) -> str:
    """Canonical-form comparison string for two DAG payloads."""
    cleaned = dict(dag)
    cleaned.pop("id", None)
    cleaned.pop("version", None)
    return json.dumps(cleaned, sort_keys=True)
