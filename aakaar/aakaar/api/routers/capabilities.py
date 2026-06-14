"""Capability listing endpoints.

Tenant users see only the capabilities granted to their tenant. Action and
control primitives are returned alongside so the UI can build the same
'available refs' list the planner sees.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from aakaar.api.deps import (
    get_registry,
    get_session,
    require_superuser,
    require_tenant_user,
)
from aakaar.api.repositories import grants as grants_repo
from aakaar.api.schemas import CapabilityDefinitionResponse, CapabilityFieldInfo
from aakaar.db.models import User
from aakaar.shared.registry import Registry
from aakaar.shared.registry.types import Definition

router = APIRouter(prefix="/capabilities", tags=["capabilities"])


@router.get("", response_model=list[CapabilityDefinitionResponse])
def list_available(
    user: Annotated[User, Depends(require_tenant_user)],
    session: Annotated[Session, Depends(get_session)],
    registry: Annotated[Registry, Depends(get_registry)],
) -> list[CapabilityDefinitionResponse]:
    assert user.tenant_id is not None
    granted = grants_repo.list_granted_refs(session, user.tenant_id)
    out: list[CapabilityDefinitionResponse] = []
    for cap in registry.capabilities():
        if cap.ref in granted:
            out.append(_serialize(cap))
    for action in registry.actions():
        out.append(_serialize(action))
    for control in registry.controls():
        out.append(_serialize(control))
    return out


@router.get("/all", response_model=list[CapabilityDefinitionResponse])
def list_all(
    _: Annotated[User, Depends(require_superuser)],
    registry: Annotated[Registry, Depends(get_registry)],
) -> list[CapabilityDefinitionResponse]:
    """Every capability/action/control in the registry, regardless of grants.
    Superuser-only — used by the cross-tenant grant UI."""
    out: list[CapabilityDefinitionResponse] = []
    for cap in registry.capabilities():
        out.append(_serialize(cap))
    for action in registry.actions():
        out.append(_serialize(action))
    for control in registry.controls():
        out.append(_serialize(control))
    return out


def _serialize(defn: Definition) -> CapabilityDefinitionResponse:
    inputs = [
        CapabilityFieldInfo(
            name=name,
            type_label=str(info.annotation),
            required=info.is_required(),
            description=info.description or "",
        )
        for name, info in defn.input_schema.model_fields.items()
    ]
    outputs = [
        CapabilityFieldInfo(
            name=name,
            type_label=str(info.annotation),
            required=info.is_required(),
            description=info.description or "",
        )
        for name, info in defn.output_schema.model_fields.items()
    ]
    secrets = [s.name for s in getattr(defn, "secrets", ())]
    tags = list(getattr(defn, "tags", ()))
    return CapabilityDefinitionResponse(
        ref=defn.ref,
        kind=defn.kind.value,
        description=defn.description,
        inputs=inputs,
        outputs=outputs,
        secret_names=secrets,
        tags=tags,
    )
