"""Persistence for remote agents. Tenant-scoped."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aakaar.db.models import RemoteAgent, RemoteAgentStatus


class AgentAliasConflict(Exception):
    """An agent with this alias already exists in the tenant."""


def create_enrollment(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    alias: str,
    api_key_hash: str,
    created_by: uuid.UUID,
    pools: list[str] | None = None,
) -> RemoteAgent:
    existing = get_by_alias(session, tenant_id=tenant_id, alias=alias)
    if existing is not None:
        raise AgentAliasConflict(f"agent alias {alias!r} already exists")
    agent = RemoteAgent(
        tenant_id=tenant_id,
        alias=alias,
        api_key_hash=api_key_hash,
        pools=pools or [],
        capabilities=[],
        status=RemoteAgentStatus.ENROLLED,
        created_by=created_by,
    )
    session.add(agent)
    session.flush()
    return agent


def list_for_tenant(session: Session, *, tenant_id: uuid.UUID) -> list[RemoteAgent]:
    stmt = (
        select(RemoteAgent)
        .where(RemoteAgent.tenant_id == tenant_id)
        .order_by(RemoteAgent.alias)
    )
    return list(session.scalars(stmt))


def get(
    session: Session, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> RemoteAgent | None:
    agent = session.get(RemoteAgent, agent_id)
    if agent is None or agent.tenant_id != tenant_id:
        return None
    return agent


def get_by_alias(
    session: Session, *, tenant_id: uuid.UUID, alias: str
) -> RemoteAgent | None:
    stmt = (
        select(RemoteAgent)
        .where(RemoteAgent.tenant_id == tenant_id)
        .where(RemoteAgent.alias == alias)
    )
    return session.scalars(stmt).first()


def delete(session: Session, *, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> bool:
    agent = get(session, tenant_id=tenant_id, agent_id=agent_id)
    if agent is None:
        return False
    session.delete(agent)
    return True


def mark_connected(
    session: Session,
    *,
    agent_id: uuid.UUID,
    os: str,
    hostname: str | None,
    gui_capable: bool,
    agent_version: str,
    capabilities: list[dict[str, Any]],
    when: datetime,
) -> None:
    agent = session.get(RemoteAgent, agent_id)
    if agent is None:
        return
    agent.os = os
    agent.hostname = hostname
    agent.gui_capable = gui_capable
    agent.agent_version = agent_version
    agent.capabilities = capabilities
    agent.status = RemoteAgentStatus.ONLINE
    agent.last_seen = when


def mark_disconnected(
    session: Session, *, agent_id: uuid.UUID, when: datetime
) -> None:
    agent = session.get(RemoteAgent, agent_id)
    if agent is None:
        return
    agent.status = RemoteAgentStatus.OFFLINE
    agent.last_seen = when
