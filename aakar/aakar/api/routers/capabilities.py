"""Capability listing endpoints.

Tenant users see only the capabilities granted to their tenant. Action and
control primitives are returned alongside so the UI can build the same
'available refs' list the planner sees.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from aakar.api.deps import (
    get_registry,
    get_session,
    require_superuser,
    require_tenant_user,
)
from aakar.api.repositories import grants as grants_repo
from aakar.api.schemas import CapabilityDefinitionResponse, CapabilityFieldInfo
from aakar.db.models import User
from aakar.shared.registry import Registry
from aakar.shared.registry.types import Definition


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
    for d in registry.capabilities():
        if d.ref in granted:
            out.append(_serialize(d))
    for d in registry.actions():
        out.append(_serialize(d))
    for d in registry.controls():
        out.append(_serialize(d))
    return out


@router.get("/all", response_model=list[CapabilityDefinitionResponse])
def list_all(
    _: Annotated[User, Depends(require_superuser)],
    registry: Annotated[Registry, Depends(get_registry)],
) -> list[CapabilityDefinitionResponse]:
    """Every capability/action/control in the registry, regardless of grants.
    Superuser-only — used by the cross-tenant grant UI."""
    out: list[CapabilityDefinitionResponse] = []
    for d in registry.capabilities():
        out.append(_serialize(d))
    for d in registry.actions():
        out.append(_serialize(d))
    for d in registry.controls():
        out.append(_serialize(d))
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
