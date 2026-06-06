"""In-process rate limiting (no Redis).

A token-bucket limiter keyed by client IP, backed by a local in-memory map.
This is single-process state — appropriate for the airgapped single-node
deployment. Auth routes get a tighter bucket to blunt brute-force/enumeration
without touching the login flow itself; everything else shares a generous
default bucket.

Keying uses the first hop of ``X-Forwarded-For`` when present (so a reverse
proxy doesn't collapse every client onto one bucket), else the socket peer.
"""

from __future__ import annotations

import time
from threading import Lock

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class TokenBucketLimiter:
    """Per-(client, rule) token bucket. Thread-safe; uses a monotonic clock."""

    def __init__(self, default_per_min: int = 240, auth_per_min: int = 20) -> None:
        self._lock = Lock()
        self._buckets: dict[str, list[float]] = {}  # key -> [tokens, last_ts]
        self._default = max(1, default_per_min)
        self._rules: tuple[tuple[str, int], ...] = (("/auth", max(1, auth_per_min)),)

    def _rule_for(self, path: str) -> tuple[str, int]:
        for prefix, per_min in self._rules:
            if path.startswith(prefix):
                return prefix, per_min
        return "default", self._default

    def check(self, client: str, path: str) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds)."""
        rule_name, per_min = self._rule_for(path)
        capacity = float(per_min)
        refill_per_sec = per_min / 60.0
        key = f"{client}:{rule_name}"
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, [capacity, now])
            tokens = min(capacity, tokens + (now - last) * refill_per_sec)
            if tokens >= 1.0:
                self._buckets[key] = [tokens - 1.0, now]
                return True, 0.0
            self._buckets[key] = [tokens, now]
            deficit = 1.0 - tokens
            return False, deficit / refill_per_sec


def _client_key(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, limiter: TokenBucketLimiter) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self._limiter = limiter

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        allowed, retry_after = self._limiter.check(
            _client_key(request), request.url.path
        )
        if not allowed:
            secs = max(1, int(retry_after) + 1)
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "detail": f"Too many requests. Retry in {secs}s.",
                },
                headers={"Retry-After": str(secs)},
            )
        response: Response = await call_next(request)
        return response


__all__ = ["RateLimitMiddleware", "TokenBucketLimiter"]
