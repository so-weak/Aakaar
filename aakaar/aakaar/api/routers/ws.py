"""WebSocket endpoint for live run events.

The client connects to ``/ws/runs/{run_id}`` and passes its JWT as a
WebSocket subprotocol (``new WebSocket(url, [token])``) rather than a query
string, so the token never lands in access logs. On connect we replay the
run's existing events (catch-up), then stream new ones from the in-process
broker until the socket closes.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from aakaar.api.auth import InvalidToken, verify_token
from aakaar.api.repositories import runs as runs_repo
from aakaar.db.models import Run

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ws"])


@router.websocket("/ws/runs/{run_id}")
async def run_events_ws(websocket: WebSocket, run_id: uuid.UUID) -> None:
    deps = websocket.app.state.deps
    settings = deps.settings

    subprotocols = list(websocket.scope.get("subprotocols") or [])
    token = subprotocols[0] if subprotocols else None
    claims = None
    if token:
        try:
            claims = verify_token(
                token, secret=settings.jwt_secret, algorithm=settings.jwt_algorithm
            )
        except InvalidToken:
            claims = None
    if claims is None:
        await websocket.close(code=4401)
        return

    # Authorize + collect catch-up events.
    with deps.session_factory.session() as s:
        run = s.get(Run, run_id)
        if run is None or (
            claims.tenant_id is not None and run.tenant_id != claims.tenant_id
        ):
            await websocket.close(code=4404)
            return
        catch_up = [
            {
                "sequence": e.sequence,
                "node_id": e.node_id,
                "kind": e.kind,
                "payload": e.payload or {},
                "at": e.at.isoformat(),
            }
            for e in runs_repo.list_events(s, run.tenant_id, run_id)
        ]

    # Echo the token subprotocol so browsers complete the handshake.
    await websocket.accept(subprotocol=token)
    queue = deps.event_broker.subscribe(run_id)
    try:
        for ev in catch_up:
            await websocket.send_json(ev)
        while True:
            ev = await queue.get()
            await websocket.send_json(ev)
    except WebSocketDisconnect:
        logger.debug("ws disconnected run_id=%s", run_id)
    except Exception:
        logger.debug("ws stream error run_id=%s", run_id, exc_info=True)
    finally:
        deps.event_broker.unsubscribe(run_id, queue)
