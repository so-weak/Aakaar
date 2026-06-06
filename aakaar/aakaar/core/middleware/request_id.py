"""Request-ID correlation.

Every inbound request gets a short correlation id (honoring an inbound
``X-Request-ID`` if the caller supplied one). The id is stored in a ContextVar
and mirrored onto the response header, and a logging filter stamps it onto
every ``LogRecord`` emitted while handling the request — so a single request's
log lines can be grepped out of an interleaved stream.
"""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_REQUEST_ID: ContextVar[str | None] = ContextVar("aakaar_request_id", default=None)

HEADER = "X-Request-ID"


def get_request_id() -> str | None:
    return _REQUEST_ID.get()


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


class RequestIdFilter(logging.Filter):
    """Inject the current request id onto log records (None when outside a request)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = _REQUEST_ID.get()
        return True


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        incoming = request.headers.get(HEADER)
        rid = incoming if incoming else _new_id()
        token = _REQUEST_ID.set(rid)
        try:
            response: Response = await call_next(request)
        finally:
            _REQUEST_ID.reset(token)
        response.headers[HEADER] = rid
        return response


__all__ = ["HEADER", "RequestIdFilter", "RequestIdMiddleware", "get_request_id"]
