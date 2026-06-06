"""http.graphql / http.soap activity tests.

These exercise the request builder (payload shape, headers, envelope
encoding) against a mock transport, and the SSRF-block path against a
private-address target. Live network calls are skipped — the SSRF guard
runs in front of the mock transport so the block path is exercised without
hitting the network.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest

from aakaar.core.net import ssrf
from aakaar.core.net.ssrf import SsrfBlocked
from aakaar.interpreter.activities import build_default_activities, http_extended
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import build_default_registry
from aakaar.storage import LocalFsObjectStore
from aakaar.vault import LocalVault


def _actx(tmp_path: Path) -> ActivityContext:
    return ActivityContext(
        tenant_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        registry=build_default_registry(),
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=LocalVault(tmp_path / "vault"),
    )


def _patch_with_mock(monkeypatch, handler) -> dict:
    """Patch the module's build_async_client so the SSRF guard wraps a mock
    transport instead of the real network transport. Returns a dict that
    captures the last request the mock saw."""
    captured: dict = {}

    def fake_handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["headers"] = dict(request.headers)
        captured["content"] = request.content
        return handler(request)

    mock_transport = httpx.MockTransport(fake_handler)

    def patched_build(**kwargs):
        allow_hosts = kwargs.get("allow_hosts", ())
        allow_private = kwargs.get("allow_private", False)
        timeout = kwargs.get("timeout", 30.0)
        # Keep the real SSRF guard in front of the mock so block paths fire.
        transport = ssrf.SsrfGuardAsyncTransport(mock_transport, allow_hosts, allow_private)
        return httpx.AsyncClient(transport=transport, timeout=timeout)

    monkeypatch.setattr(
        "aakaar.interpreter.activities.http_extended.build_async_client", patched_build
    )
    return captured


# ---------- registration --------------------------------------------------


def test_http_extended_registered() -> None:
    reg = build_default_activities()
    assert "http.graphql" in reg
    assert "http.soap" in reg


# ---------- graphql builder ------------------------------------------------


@pytest.mark.asyncio
async def test_graphql_builds_payload_and_returns_data(monkeypatch, tmp_path: Path) -> None:
    import json

    activities = build_default_activities()
    actx = _actx(tmp_path)
    captured = _patch_with_mock(
        monkeypatch,
        lambda req: httpx.Response(200, json={"data": {"viewer": {"id": "u1"}}, "errors": None}),
    )

    handler = activities.get("http.graphql")
    assert handler is not None
    out = await handler(
        actx,
        {
            "url": "https://api.example.com/graphql",
            "query": "query Q($id: ID!) { viewer(id: $id) { id } }",
            "variables": {"id": "u1"},
            "operation_name": "Q",
            # Allowlist the host so the guard skips DNS resolution (the test
            # runs offline); the block-path tests below use IP literals so
            # the SSRF guard still fires there.
            "allow_hosts": ["api.example.com"],
        },
    )

    assert out["status"] == 200
    assert out["data"] == {"viewer": {"id": "u1"}}
    assert out["errors"] is None

    body = json.loads(captured["content"])
    assert body["query"].startswith("query Q")
    assert body["variables"] == {"id": "u1"}
    assert body["operationName"] == "Q"
    assert captured["headers"]["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_graphql_surfaces_errors(monkeypatch, tmp_path: Path) -> None:
    activities = build_default_activities()
    actx = _actx(tmp_path)
    _patch_with_mock(
        monkeypatch,
        lambda req: httpx.Response(200, json={"data": None, "errors": [{"message": "boom"}]}),
    )
    handler = activities.get("http.graphql")
    assert handler is not None
    out = await handler(
        actx,
        {
            "url": "https://api.example.com/graphql",
            "query": "{ x }",
            "allow_hosts": ["api.example.com"],
        },
    )
    assert out["data"] is None
    assert out["errors"] == [{"message": "boom"}]


@pytest.mark.asyncio
async def test_graphql_ssrf_block(monkeypatch, tmp_path: Path) -> None:
    activities = build_default_activities()
    actx = _actx(tmp_path)
    _patch_with_mock(monkeypatch, lambda req: httpx.Response(200, json={"data": {}}))
    handler = activities.get("http.graphql")
    assert handler is not None
    with pytest.raises(SsrfBlocked):
        await handler(actx, {"url": "http://127.0.0.1/graphql", "query": "{ x }"})


@pytest.mark.asyncio
async def test_graphql_ssrf_allow_private(monkeypatch, tmp_path: Path) -> None:
    activities = build_default_activities()
    actx = _actx(tmp_path)
    _patch_with_mock(monkeypatch, lambda req: httpx.Response(200, json={"data": {"ok": 1}}))
    handler = activities.get("http.graphql")
    assert handler is not None
    out = await handler(
        actx,
        {"url": "http://127.0.0.1/graphql", "query": "{ x }", "allow_private": True},
    )
    assert out["status"] == 200
    assert out["data"] == {"ok": 1}


# ---------- soap builder ---------------------------------------------------


@pytest.mark.asyncio
async def test_soap_posts_envelope_with_action(monkeypatch, tmp_path: Path) -> None:
    activities = build_default_activities()
    actx = _actx(tmp_path)
    captured = _patch_with_mock(
        monkeypatch,
        lambda req: httpx.Response(200, text="<soap:Envelope>resp</soap:Envelope>"),
    )

    envelope = "<soap:Envelope><soap:Body><Ping/></soap:Body></soap:Envelope>"
    handler = activities.get("http.soap")
    assert handler is not None
    out = await handler(
        actx,
        {
            "url": "https://soap.example.com/service",
            "envelope": envelope,
            "soap_action": "urn:Ping",
            "allow_hosts": ["soap.example.com"],
        },
    )

    assert out["status"] == 200
    assert out["body"] == "<soap:Envelope>resp</soap:Envelope>"
    assert captured["method"] == "POST"
    assert captured["headers"]["soapaction"] == "urn:Ping"
    assert "text/xml" in captured["headers"]["content-type"]
    assert captured["content"] == envelope.encode("utf-8")


@pytest.mark.asyncio
async def test_soap_rejects_non_string_envelope(monkeypatch, tmp_path: Path) -> None:
    activities = build_default_activities()
    actx = _actx(tmp_path)
    _patch_with_mock(monkeypatch, lambda req: httpx.Response(200, text="ok"))
    handler = activities.get("http.soap")
    assert handler is not None
    with pytest.raises(ValueError, match="envelope"):
        await handler(actx, {"url": "https://soap.example.com/s", "envelope": {"not": "xml"}})


@pytest.mark.asyncio
async def test_soap_ssrf_block(monkeypatch, tmp_path: Path) -> None:
    activities = build_default_activities()
    actx = _actx(tmp_path)
    _patch_with_mock(monkeypatch, lambda req: httpx.Response(200, text="ok"))
    handler = activities.get("http.soap")
    assert handler is not None
    with pytest.raises(SsrfBlocked):
        await handler(
            actx,
            {"url": "http://169.254.169.254/latest/meta-data", "envelope": "<x/>"},
        )


# ---------- module-level helper smoke -------------------------------------


def test_ssrf_kwargs_helper_defaults() -> None:
    kwargs = http_extended._ssrf_kwargs({}, 5000)
    assert kwargs == {"allow_hosts": (), "allow_private": False, "timeout": 5.0}


@pytest.mark.skip(reason="live network call; enable manually for integration checks")
@pytest.mark.asyncio
async def test_graphql_live(tmp_path: Path) -> None:  # pragma: no cover
    activities = build_default_activities()
    actx = _actx(tmp_path)
    handler = activities.get("http.graphql")
    assert handler is not None
    out = await handler(
        actx,
        {"url": "https://countries.trevorblades.com/", "query": "{ countries { code } }"},
    )
    assert out["status"] == 200
