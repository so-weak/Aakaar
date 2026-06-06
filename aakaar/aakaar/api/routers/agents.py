"""Remote-agent management (REST) + the agent WebSocket endpoint.

REST (tenant-admin): enroll / list / revoke agents, and a placement pre-flight
check for a DAG. WebSocket (`/ws/agents`): an enrolled agent dials in, is
authenticated by its agent-id + one-time-enrolled key, announces its OS / GUI
session / capabilities, and is registered as a live connection the dispatcher
can target. Result/event frames from the agent are routed back here.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from sqlalchemy.orm import Session

from aakaar.api.auth.passwords import hash_password, verify_password
from aakaar.api.deps import (
    get_agent_registry,
    get_audit,
    get_registry,
    get_session,
    require_tenant_admin,
    require_tenant_user,
)
from aakaar.api.repositories import agents as agents_repo
from aakaar.api.schemas import (
    AgentEnrollRequest,
    AgentEnrollResponse,
    AgentResponse,
    PlacementCheckResponse,
)
from aakaar.db.models import RemoteAgent, User
from aakaar.services.audit import AuditRecorder
from aakaar.shared.dag.types import Dag
from aakaar.shared.registry import Registry
from aakaar.workers.remote import (
    AgentRegistry,
    WebSocketAgentConnection,
    check_placement,
    parse_hello,
)
from aakaar.workers.remote.protocol import RemoteResult

logger = logging.getLogger(__name__)
router = APIRouter(tags=["agents"])


def _to_response(agent: RemoteAgent, *, online: bool) -> AgentResponse:
    resp = AgentResponse.model_validate(agent)
    resp.online = online
    return resp


@router.post(
    "/agents/enroll",
    response_model=AgentEnrollResponse,
    status_code=status.HTTP_201_CREATED,
)
def enroll_agent(
    body: AgentEnrollRequest,
    admin: Annotated[User, Depends(require_tenant_admin)],
    session: Annotated[Session, Depends(get_session)],
    audit: Annotated[AuditRecorder, Depends(get_audit)],
) -> AgentEnrollResponse:
    assert admin.tenant_id is not None
    key = secrets.token_urlsafe(32)
    try:
        agent = agents_repo.create_enrollment(
            session,
            tenant_id=admin.tenant_id,
            alias=body.alias,
            api_key_hash=hash_password(key),
            created_by=admin.id,
            pools=body.pools,
        )
    except agents_repo.AgentAliasConflict as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    session.commit()
    audit.record(
        action="agent.enroll",
        tenant_id=admin.tenant_id,
        actor_id=admin.id,
        target_kind="agent",
        target_id=str(agent.id),
        payload={"alias": body.alias, "pools": body.pools},
    )
    # The key embeds the agent id so the agent can identify itself at connect:
    # "<agent_id>.<secret>". Only the hash of the secret is stored.
    return AgentEnrollResponse(
        id=agent.id,
        alias=agent.alias,
        agent_id=agent.id,
        enrollment_key=f"{agent.id}.{key}",
    )


@router.get("/agents", response_model=list[AgentResponse])
def list_agents(
    admin: Annotated[User, Depends(require_tenant_admin)],
    session: Annotated[Session, Depends(get_session)],
    registry: Annotated[AgentRegistry, Depends(get_agent_registry)],
) -> list[AgentResponse]:
    assert admin.tenant_id is not None
    online = {info.alias for info in registry.list_online(admin.tenant_id)}
    return [
        _to_response(a, online=a.alias in online)
        for a in agents_repo.list_for_tenant(session, tenant_id=admin.tenant_id)
    ]


@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_agent(
    agent_id: uuid.UUID,
    admin: Annotated[User, Depends(require_tenant_admin)],
    session: Annotated[Session, Depends(get_session)],
    registry: Annotated[AgentRegistry, Depends(get_agent_registry)],
    audit: Annotated[AuditRecorder, Depends(get_audit)],
) -> None:
    assert admin.tenant_id is not None
    agent = agents_repo.get(session, tenant_id=admin.tenant_id, agent_id=agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    alias = agent.alias
    agents_repo.delete(session, tenant_id=admin.tenant_id, agent_id=agent_id)
    session.commit()
    registry.unregister(admin.tenant_id, alias)  # drop the live connection if any
    audit.record(
        action="agent.revoke",
        tenant_id=admin.tenant_id,
        actor_id=admin.id,
        target_kind="agent",
        target_id=str(agent_id),
        payload={"alias": alias},
    )


@router.post("/placement/check", response_model=PlacementCheckResponse)
def placement_check(
    dag: Dag,
    user: Annotated[User, Depends(require_tenant_user)],
    registry: Annotated[AgentRegistry, Depends(get_agent_registry)],
    capabilities: Annotated[Registry, Depends(get_registry)],
) -> PlacementCheckResponse:
    assert user.tenant_id is not None
    issues = check_placement(
        dag, user.tenant_id, agents=registry, registry=capabilities
    )
    return PlacementCheckResponse(
        issues=issues, online_agents=len(registry.list_online(user.tenant_id))
    )


# ---------- agent WebSocket --------------------------------------------------


def _parse_key(raw: str | None) -> tuple[uuid.UUID, str] | None:
    if not raw or "." not in raw:
        return None
    agent_id_str, _, secret = raw.partition(".")
    try:
        return uuid.UUID(agent_id_str), secret
    except ValueError:
        return None


@router.websocket("/ws/agents")
async def agent_ws(websocket: WebSocket) -> None:
    deps = websocket.app.state.deps
    if not deps.settings.remote_exec_enabled:
        await websocket.close(code=4403)
        return

    parsed = _parse_key(websocket.headers.get("x-agent-key"))
    if parsed is None:
        await websocket.close(code=4401)
        return
    agent_id, secret = parsed

    with deps.session_factory.session() as s:
        agent = s.get(RemoteAgent, agent_id)
        if agent is None or not verify_password(secret, agent.api_key_hash):
            await websocket.close(code=4401)
            return
        tenant_id = agent.tenant_id
        alias = agent.alias
        pools = tuple(agent.pools or [])

    await websocket.accept()
    try:
        hello = await websocket.receive_json()
    except Exception:
        await websocket.close(code=4400)
        return
    info = parse_hello(hello, alias=alias, tenant_id=tenant_id)
    info.pools = pools  # pools are admin-controlled at enrollment, not agent-claimed

    with deps.session_factory.session() as s:
        agents_repo.mark_connected(
            s,
            agent_id=agent_id,
            os=info.os,
            hostname=info.hostname,
            gui_capable=info.gui_capable,
            agent_version=info.version,
            capabilities=[{"ref": c.ref, "version": c.version} for c in info.capabilities],
            when=datetime.now(UTC),
        )
        s.commit()

    conn = WebSocketAgentConnection(websocket, info)
    deps.agent_registry.register(conn)
    logger.info("agent connected alias=%s tenant=%s os=%s", alias, tenant_id, info.os)
    await websocket.send_json({"type": "welcome", "alias": alias})
    try:
        while True:
            msg = await websocket.receive_json()
            kind = msg.get("type")
            if kind == "result":
                conn.resolve_result(msg)
            elif kind == "event":
                _relay_event(deps, tenant_id, msg)
            # ping/pong and anything else: ignore (WS keepalive handles liveness)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("agent ws error alias=%s", alias, exc_info=True)
    finally:
        conn.fail_pending("agent disconnected")
        deps.agent_registry.unregister(tenant_id, alias)
        with deps.session_factory.session() as s:
            agents_repo.mark_disconnected(s, agent_id=agent_id, when=datetime.now(UTC))
            s.commit()


def _relay_event(deps: object, tenant_id: uuid.UUID, msg: dict) -> None:
    try:
        run_id = uuid.UUID(str(msg["run_id"]))
        deps.event_recorder.record(  # type: ignore[attr-defined]
            run_id=run_id,
            tenant_id=tenant_id,
            node_id=msg.get("node_id"),
            kind=str(msg.get("kind", "log")),
            payload=msg.get("payload") or {},
        )
    except Exception:
        logger.debug("agent event relay failed", exc_info=True)


# Keep the RemoteResult import referenced for type clarity in this module.
_ = RemoteResult
