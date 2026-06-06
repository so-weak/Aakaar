"""Prometheus metrics.

Exposes a local ``/metrics`` endpoint (scraped on the host / LAN — no external
egress). We deliberately do NOT enable OpenTelemetry export, which would need a
collector the airgapped target does not have.

Metrics:
- ``aakaar_http_requests_total{method,path,status}`` — request counter
- ``aakaar_http_request_duration_seconds{method,path}`` — latency histogram
- ``aakaar_runs_total{status}`` — workflow run outcomes (incremented by the
  orchestrator via :func:`record_run_outcome`)
"""

from __future__ import annotations

import contextlib
import time

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

HTTP_REQUESTS = Counter(
    "aakaar_http_requests_total",
    "Total HTTP requests.",
    labelnames=("method", "path", "status"),
)
HTTP_LATENCY = Histogram(
    "aakaar_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    labelnames=("method", "path"),
)
RUNS_TOTAL = Counter(
    "aakaar_runs_total",
    "Workflow run outcomes.",
    labelnames=("status",),
)


def record_run_outcome(status: str) -> None:
    """Increment the run-outcome counter (called by the orchestrator)."""
    with contextlib.suppress(Exception):  # pragma: no cover - metrics must never break a run
        RUNS_TOTAL.labels(status=status).inc()


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path or request.url.path


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        start = time.perf_counter()
        status = 500
        try:
            response: Response = await call_next(request)
            status = response.status_code
            return response
        finally:
            path = _route_template(request)
            elapsed = time.perf_counter() - start
            HTTP_LATENCY.labels(method=request.method, path=path).observe(elapsed)
            HTTP_REQUESTS.labels(
                method=request.method, path=path, status=str(status)
            ).inc()


def metrics_response() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


__all__ = [
    "MetricsMiddleware",
    "metrics_response",
    "record_run_outcome",
]
