"""Object-store proxy endpoint.

The HITL captcha flow stores the captcha image to managed storage and
references it by `aakar://t/{tenant_id}/...` in the prompt message. The
chat UI needs to display the image inline; browsers don't speak the
`aakar://` scheme. This endpoint translates a managed-storage URI to its
bytes, gated on tenant ownership.

Strict cross-tenant guarantee: the URI's tenant prefix must match the
caller's tenant. Superusers are explicitly NOT given the ability to read
arbitrary tenant blobs through this endpoint; that's a deliberate
defense-in-depth choice — captcha bytes can contain personally
identifying portal context.
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from aakar.api.deps import get_object_store, require_tenant_user
from aakar.db.models import User
from aakar.storage.object_store import ObjectStorage


router = APIRouter(prefix="/objects", tags=["objects"])


_URI_RE = re.compile(r"^aakar://t/([0-9a-f-]{36})/(.+)$")


@router.get("")
def get_object(
    uri: Annotated[str, Query(min_length=1, max_length=2048)],
    user: Annotated[User, Depends(require_tenant_user)],
    object_store: Annotated[ObjectStorage, Depends(get_object_store)],
) -> Response:
    """Return raw bytes for a managed-storage URI scoped to the caller's tenant."""
    assert user.tenant_id is not None
    match = _URI_RE.match(uri)
    if match is None:
        raise HTTPException(status_code=400, detail="invalid aakar:// uri")
    if match.group(1) != str(user.tenant_id):
        raise HTTPException(status_code=403, detail="cross-tenant access denied")
    try:
        data = object_store.get(uri)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail="object not found") from e
    content_type = _content_type_for(uri)
    return Response(content=data, media_type=content_type)


def _content_type_for(uri: str) -> str:
    lower = uri.lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".jpg") or lower.endswith(".jpeg"):
        return "image/jpeg"
    if lower.endswith(".gif"):
        return "image/gif"
    if lower.endswith(".webp"):
        return "image/webp"
    if lower.endswith(".svg"):
        return "image/svg+xml"
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith(".csv"):
        return "text/csv"
    if lower.endswith(".json"):
        return "application/json"
    if lower.endswith(".txt"):
        return "text/plain"
    return "application/octet-stream"
