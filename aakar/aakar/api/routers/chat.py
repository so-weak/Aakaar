"""Chat → planner endpoint.

POST /chat — one turn of NL planning. Returns a `ChatResponse` (one of
dag/clarify/missing). The chat history is the client's responsibility for
v1; the planner is stateless. We may add a stored chat session later.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from aakar.api.deps import get_planner, get_session, require_tenant_user
from aakar.api.repositories import grants as grants_repo
from aakar.api.repositories import workflows as workflows_repo
from aakar.api.schemas import ChatRequest, ChatResponse
from aakar.db.models import User
from aakar.planner import PlannerError, PlannerService
from aakar.shared.dag.types import Dag
from aakar.shared.planner.responses import (
    ClarifyResponse,
    DagResponse,
    MissingResponse,
)


router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
def chat_turn(
    body: ChatRequest,
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
    planner: Annotated[PlannerService, Depends(get_planner)],
) -> ChatResponse:
    assert user.tenant_id is not None

    # If editing a saved workflow, prefer the persisted DAG over a client-supplied one.
    current_dag: Dag | None = body.current_dag
    if body.workflow_id is not None and current_dag is None:
        version = workflows_repo.get_latest_version(session, user.tenant_id, body.workflow_id)
        if version is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        current_dag = Dag.model_validate(version.dag)

    granted = grants_repo.list_granted_refs(session, user.tenant_id)

    try:
        resp = planner.plan(
            user_message=body.message,
            granted_capabilities=granted,
            current_dag=current_dag,
        )
    except PlannerError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"planner failed: {e}",
        ) from e

    if isinstance(resp, DagResponse):
        return ChatResponse(kind="dag", rationale=resp.rationale, dag=resp.dag)
    if isinstance(resp, ClarifyResponse):
        return ChatResponse(kind="clarify", questions=list(resp.questions))
    assert isinstance(resp, MissingResponse)
    return ChatResponse(
        kind="missing",
        needed=list(resp.needed),
        explanation=resp.explanation,
    )
