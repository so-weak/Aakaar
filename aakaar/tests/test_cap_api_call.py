"""Tests for cap.api_call.

Live HTTP isn't available in unit tests, so we:
  - exercise the pure auth-header builder + header-merge helpers directly,
  - assert input validation (method / URL),
  - assert SsrfBlocked is raised for a private (loopback) URL via the real
    SSRF-guarded client, and
  - drive the full async handler against an httpx.MockTransport (no socket),
    verifying auth headers, query params, JSON body, and grant-supplied
    allow_hosts / api_key_header all flow through.
"""

from __future__ import annotations

import base64
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from aakaar.capabilities.integration.api_call import (
    CAP_REF,
    build_auth_headers,
    definition,
    handler,
    merge_headers,
)
from aakaar.core.net.ssrf import SsrfBlocked
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import build_default_registry
from aakaar.storage import LocalFsObjectStore
from aakaar.vault import LocalVault

# ---------- definition + pure helpers --------------------------------------


def test_definition_shape() -> None:
    assert definition.ref == CAP_REF == "cap.api_call"
    names = {s.name for s in definition.secrets}
    assert names == {"token", "api_key", "username", "password"}


def test_build_auth_headers_bearer_wins() -> None:
    h = build_auth_headers({"token": " t0k ", "api_key": "k", "username": "u", "password": "p"})
    assert h == {"Authorization": "Bearer t0k"}


def test_build_auth_headers_api_key_default_and_custom_header() -> None:
    assert build_auth_headers({"api_key": "abc"}) == {"X-API-Key": "abc"}
    assert build_auth_headers({"api_key": "abc"}, api_key_header="X-Token") == {"X-Token": "abc"}
    # blank header name falls back to default
    assert build_auth_headers({"api_key": "abc"}, api_key_header="  ") == {"X-API-Key": "abc"}


def test_build_auth_headers_basic() -> None:
    h = build_auth_headers({"username": "alice", "password": "s3cr3t"})
    expected = "Basic " + base64.b64encode(b"alice:s3cr3t").decode("ascii")
    assert h == {"Authorization": expected}


def test_build_auth_headers_empty_when_no_secret() -> None:
    assert build_auth_headers({}) == {}
    assert build_auth_headers({"token": "  ", "api_key": ""}) == {}


def test_merge_headers_caller_wins_case_insensitively() -> None:
    base = {"Authorization": "Bearer x", "X-API-Key": "k"}
    out = merge_headers(base, {"authorization": "Bearer override"})
    # the caller's casing replaces the base key (no duplicate Authorization)
    assert out == {"authorization": "Bearer override", "X-API-Key": "k"}
    # base is not mutated
    assert base["Authorization"] == "Bearer x"


def test_merge_headers_none_override() -> None:
    assert merge_headers({"A": "1"}, None) == {"A": "1"}


# ---------- ctx builder ----------------------------------------------------


def _ctx(
    tmp_path: Path,
    *,
    defaults: dict[str, Any] | None = None,
    secrets: dict[str, str] | None = None,
    grant: bool = True,
) -> ActivityContext:
    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    granted: dict[str, Any] = {}
    if grant:
        vault_ref = f"grants/{uuid.uuid4()}"
        vault.put(str(tenant_id), vault_ref, secrets or {})
        granted = {
            CAP_REF: {
                "primary": {"vault_ref": vault_ref, "input_defaults": defaults or {}}
            }
        }
    return ActivityContext(
        tenant_id=tenant_id,
        run_id=uuid.uuid4(),
        registry=build_default_registry(),
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=vault,
        granted_capabilities=granted,
    )


# ---------- input validation -----------------------------------------------


@pytest.mark.asyncio
async def test_handler_rejects_bad_method(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, grant=False)
    with pytest.raises(ValueError, match="unsupported method"):
        await handler(ctx, {"method": "FETCH", "url": "https://api.x.test/v1"})


@pytest.mark.asyncio
async def test_handler_rejects_non_absolute_url(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, grant=False)
    with pytest.raises(ValueError, match="absolute http"):
        await handler(ctx, {"method": "GET", "url": "/relative/path"})


@pytest.mark.asyncio
async def test_handler_blocks_private_url(tmp_path: Path) -> None:
    """The real SSRF-guarded client must refuse a loopback target."""
    ctx = _ctx(tmp_path, grant=False)
    with pytest.raises(SsrfBlocked):
        await handler(ctx, {"method": "GET", "url": "http://127.0.0.1:9/internal"})


# ---------- handler happy path (httpx.MockTransport) -----------------------


def _patch_client(monkeypatch: pytest.MonkeyPatch, capture: dict[str, Any]) -> None:
    """Replace build_async_client in the cap module with a MockTransport-backed
    client so no socket is opened. Records the allow_hosts it was built with."""

    def _build(*, allow_hosts: Any = (), timeout: float = 30.0, **_: Any) -> httpx.AsyncClient:
        capture["allow_hosts"] = list(allow_hosts)
        capture["timeout"] = timeout

        async def _respond(request: httpx.Request) -> httpx.Response:
            capture["request"] = request
            capture["request_content"] = request.content
            return httpx.Response(
                201,
                headers={"Content-Type": "application/json", "X-Trace": "t1"},
                json={"ok": True, "echo": "hi"},
            )

        return httpx.AsyncClient(transport=httpx.MockTransport(_respond), timeout=timeout)

    import aakaar.capabilities.integration.api_call as mod

    monkeypatch.setattr(mod, "build_async_client", _build)


@pytest.mark.asyncio
async def test_handler_unauthenticated_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture: dict[str, Any] = {}
    _patch_client(monkeypatch, capture)

    ctx = _ctx(tmp_path, grant=False)
    out = await handler(
        ctx,
        {
            "method": "get",  # lower-case normalised
            "url": "https://api.x.test/v1/items",
            "query": {"page": "2"},
        },
    )
    assert out["status"] == 201
    assert out["body"] == {"ok": True, "echo": "hi"}
    assert out["headers"]["x-trace"] == "t1"

    req: httpx.Request = capture["request"]
    assert req.method == "GET"
    assert req.url.params.get("page") == "2"
    # no auth header on an unauthenticated call
    assert "authorization" not in {k.lower() for k in req.headers}


@pytest.mark.asyncio
async def test_handler_bearer_with_json_body_and_allow_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture: dict[str, Any] = {}
    _patch_client(monkeypatch, capture)

    ctx = _ctx(
        tmp_path,
        defaults={"allow_hosts": ["svc.lan.test"]},
        secrets={"token": "abc123"},
    )
    out = await handler(
        ctx,
        {
            "method": "POST",
            "url": "http://svc.lan.test/api/run",
            "account_alias": "primary",
            "json_body": {"name": "widget"},
            "allow_hosts": ["other.lan.test"],
        },
    )
    assert out["status"] == 201

    req: httpx.Request = capture["request"]
    assert req.method == "POST"
    assert req.headers["authorization"] == "Bearer abc123"
    assert b'"name"' in capture["request_content"]
    assert req.headers["content-type"].startswith("application/json")
    # node + grant allow_hosts are unioned and passed to the client builder
    assert set(capture["allow_hosts"]) == {"svc.lan.test", "other.lan.test"}


@pytest.mark.asyncio
async def test_handler_api_key_custom_header_from_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture: dict[str, Any] = {}
    _patch_client(monkeypatch, capture)

    ctx = _ctx(
        tmp_path,
        defaults={"api_key_header": "X-Token"},
        secrets={"api_key": "KEY9"},
    )
    await handler(
        ctx,
        {"method": "GET", "url": "https://api.x.test/v1", "account_alias": "primary"},
    )
    req: httpx.Request = capture["request"]
    assert req.headers["x-token"] == "KEY9"


@pytest.mark.asyncio
async def test_handler_caller_header_overrides_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture: dict[str, Any] = {}
    _patch_client(monkeypatch, capture)

    ctx = _ctx(tmp_path, secrets={"token": "fromvault"})
    await handler(
        ctx,
        {
            "method": "GET",
            "url": "https://api.x.test/v1",
            "account_alias": "primary",
            "headers": {"Authorization": "Bearer caller"},
        },
    )
    req: httpx.Request = capture["request"]
    assert req.headers["authorization"] == "Bearer caller"


@pytest.mark.asyncio
async def test_handler_missing_grant_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, grant=False)
    with pytest.raises(PermissionError):
        await handler(
            ctx,
            {"method": "GET", "url": "https://api.x.test/v1", "account_alias": "primary"},
        )
