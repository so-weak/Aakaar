"""Exception handlers.

The default FastAPI handlers are good but their error envelopes vary by
exception class. We standardize on `{ "error": <code>, "detail": <msg> }`
across the board so the frontend has one shape to parse.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


logger = logging.getLogger(__name__)


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # 4xx is expected client error; 5xx is a real server problem worth
        # surfacing at WARN/ERROR so it shows up under default log levels.
        if exc.status_code >= 500:
            logger.error(
                "http %s on %s %s: %s",
                exc.status_code,
                request.method,
                request.url.path,
                exc.detail,
            )
        else:
            logger.debug(
                "http %s on %s %s: %s",
                exc.status_code,
                request.method,
                request.url.path,
                exc.detail,
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": _slug_for(exc.status_code), "detail": str(exc.detail)},
            headers=exc.headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        detail = _format_validation(exc)
        logger.info(
            "request validation failed %s %s: %s",
            request.method,
            request.url.path,
            detail,
        )
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "detail": detail,
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unhandled exception on %s %s: %s",
            request.method,
            request.url.path,
            exc,
        )
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "detail": "internal server error"},
        )


def _slug_for(code: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        422: "validation_error",
        502: "upstream_error",
    }.get(code, "error")


def _format_validation(exc: RequestValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg', '')}")
    return "; ".join(parts) or "invalid request"
