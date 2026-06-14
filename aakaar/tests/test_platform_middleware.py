"""Tests for the cross-cutting platform middleware: request-id, metrics,
rate limiting, and the SSRF guard."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aakaar.core.middleware.rate_limit import TokenBucketLimiter
from aakaar.core.net.ssrf import SsrfBlocked, assert_host_allowed


def test_request_id_generated_and_echoed(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.headers.get("X-Request-ID")


def test_request_id_honors_inbound(client: TestClient) -> None:
    r = client.get("/healthz", headers={"X-Request-ID": "abc123"})
    assert r.headers.get("X-Request-ID") == "abc123"


def test_metrics_endpoint(client: TestClient) -> None:
    client.get("/healthz")
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "aakaar_http_requests_total" in r.text


def test_token_bucket_blocks_after_capacity() -> None:
    limiter = TokenBucketLimiter(default_per_min=240, auth_per_min=2)
    ok1, _ = limiter.check("1.2.3.4", "/auth/login")
    ok2, _ = limiter.check("1.2.3.4", "/auth/login")
    ok3, retry = limiter.check("1.2.3.4", "/auth/login")
    assert ok1 and ok2
    assert not ok3
    assert retry > 0


def test_token_bucket_isolates_clients() -> None:
    limiter = TokenBucketLimiter(default_per_min=240, auth_per_min=1)
    assert limiter.check("a", "/auth/login")[0]
    # Different client still has its own bucket.
    assert limiter.check("b", "/auth/login")[0]


def test_ssrf_blocks_loopback_and_private() -> None:
    with pytest.raises(SsrfBlocked):
        assert_host_allowed("127.0.0.1")
    with pytest.raises(SsrfBlocked):
        assert_host_allowed("169.254.169.254")  # cloud metadata


def test_ssrf_allows_public_ip() -> None:
    # Numeric, public — resolves without DNS and passes.
    assert assert_host_allowed("8.8.8.8") is None


def test_ssrf_allowlist_and_allow_private() -> None:
    assert assert_host_allowed("127.0.0.1", allow_private=True) is None
    assert assert_host_allowed("127.0.0.1", allow_hosts=["127.0.0.1"]) is None
