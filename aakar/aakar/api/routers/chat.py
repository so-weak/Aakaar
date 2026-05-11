"""Chat → planner endpoint.

POST /chat — one turn of NL planning. Returns a `ChatResponse` (one of
dag/clarify/missing). The chat history is the client's responsibility for
v1; the planner is stateless. We may add a stored chat session later.
"""

from __future__ import annotations

import logging
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


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
def chat_turn(
    body: ChatRequest,
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
    planner: Annotated[PlannerService, Depends(get_planner)],
) -> ChatResponse:
    assert user.tenant_id is not None
    logger.info(
        "chat turn tenant_id=%s user_id=%s workflow_id=%s message_len=%d has_current_dag=%s",
        user.tenant_id,
        user.id,
        body.workflow_id,
        len(body.message or ""),
        body.current_dag is not None,
    )

    # If editing a saved workflow, prefer the persisted DAG over a client-supplied one.
    current_dag: Dag | None = body.current_dag
    if body.workflow_id is not None and current_dag is None:
        version = workflows_repo.get_latest_version(session, user.tenant_id, body.workflow_id)
        if version is None:
            logger.warning("chat: workflow_id=%s not found", body.workflow_id)
            raise HTTPException(status_code=404, detail="workflow not found")
        current_dag = Dag.model_validate(version.dag)

    granted = grants_repo.list_granted_refs(session, user.tenant_id)
    granted_aliases: dict[str, list[str]] = {}
    for g in grants_repo.list_grants(session, user.tenant_id):
        if g.enabled:
            granted_aliases.setdefault(g.capability_ref, []).append(g.account_alias)
    for refs in granted_aliases.values():
        refs.sort()

    try:
        resp = planner.plan(
            user_message=body.message,
            granted_capabilities=granted,
            granted_aliases=granted_aliases,
            current_dag=current_dag,
        )
    except PlannerError as e:
        logger.exception("planner failed for tenant_id=%s user_id=%s", user.tenant_id, user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"planner failed: {e}",
        ) from e

    if isinstance(resp, DagResponse):
        logger.info("chat -> dag (nodes=%d)", len(resp.dag.nodes))
        return ChatResponse(kind="dag", rationale=resp.rationale, dag=resp.dag)
    if isinstance(resp, ClarifyResponse):
        logger.info("chat -> clarify (questions=%d)", len(resp.questions))
        return ChatResponse(kind="clarify", questions=list(resp.questions))
    assert isinstance(resp, MissingResponse)
    logger.info("chat -> missing capabilities (needed=%d)", len(resp.needed))
    return ChatResponse(
        kind="missing",
        needed=list(resp.needed),
        explanation=resp.explanation,
    )
