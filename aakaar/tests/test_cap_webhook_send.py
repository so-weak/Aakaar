"""Tests for cap.webhook_send.

A live outbound POST isn't available (and the airgapped target has no
public internet), so we:
  - exercise the pure request-builder helper directly (no I/O),
  - assert the handler blocks loopback / private targets via SsrfBlocked,
  - assert scheme + payload validation, and
  - cover a successful POST by monkeypatching httpx.AsyncClient.post so no
    socket is ever opened.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from aakaar.capabilities.integration.webhook_send import (
    CAP_REF,
    build_request_kwargs,
    definition,
    handler,
)
from aakaar.core.net.ssrf import SsrfBlocked
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import build_default_registry
from aakaar.storage import LocalFsObjectStore
from aakaar.vault import LocalVault

# ---------- definition + pure helpers --------------------------------------


def test_definition_shape() -> None:
    assert definition.ref == CAP_REF == "cap.webhook_send"
    # No credentials of its own.
    assert definition.secrets == ()
    assert set(definition.input_schema.model_fields) == {
        "url",
        "payload",
        "headers",
        "allow_hosts",
        "timeout_s",
    }
    assert set(definition.output_schema.model_fields) == {"status", "body"}


def test_build_request_kwargs_defaults_json_content_type() -> None:
    kw = build_request_kwargs({"a": 1, "b": "x"}, None)
    assert kw["headers"]["Content-Type"] == "application/json"
    assert json.loads(kw["content"].decode("utf-8")) == {"a": 1, "b": "x"}


def test_build_request_kwargs_caller_headers_win() -> None:
    kw = build_request_kwargs(
        {"k": "v"},
        {"Authorization": "Bearer t", "Content-Type": "application/json; charset=utf-8"},
    )
    assert kw["headers"]["Authorization"] == "Bearer t"
    assert kw["headers"]["Content-Type"] == "application/json; charset=utf-8"


def test_build_request_kwargs_non_serializable_falls_back_to_str() -> None:
    # default=str keeps the builder from blowing up on odd types.
    kw = build_request_kwargs({"when": uuid.uuid4()}, None)
    body = json.loads(kw["content"].decode("utf-8"))
    assert isinstance(body["when"], str)


# ---------- ctx ------------------------------------------------------------


def _ctx(tmp_path: Path) -> ActivityContext:
    tenant_id = uuid.uuid4()
    return ActivityContext(
        tenant_id=tenant_id,
        run_id=uuid.uuid4(),
        registry=build_default_registry(),
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=LocalVault(tmp_path / "vault"),
    )


# ---------- SSRF + validation ----------------------------------------------


@pytest.mark.asyncio
async def test_blocks_loopback(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(SsrfBlocked):
        await handler(
            ctx,
            {"url": "http://127.0.0.1:8080/hook", "payload": {"x": 1}},
        )


@pytest.mark.asyncio
async def test_blocks_private_ip(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(SsrfBlocked):
        await handler(
            ctx,
            {"url": "http://10.0.0.5/notify", "payload": {"x": 1}},
        )


@pytest.mark.asyncio
async def test_blocks_link_local_metadata(tmp_path: Path) -> None:
    # The classic cloud-metadata endpoint.
    ctx = _ctx(tmp_path)
    with pytest.raises(SsrfBlocked):
        await handler(
            ctx,
            {"url": "http://169.254.169.254/latest/meta-data/", "payload": {}},
        )


@pytest.mark.asyncio
async def test_allow_hosts_lets_private_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With the host allow-listed, the early SSRF check passes; we stub the
    # actual POST so no socket opens.
    ctx = _ctx(tmp_path)
    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 202
        text = "queued"

    async def _fake_post(self: Any, url: str, **kwargs: Any) -> _Resp:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _Resp()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    out = await handler(
        ctx,
        {
            "url": "http://10.0.0.5/notify",
            "payload": {"event": "ping"},
            "allow_hosts": ["10.0.0.5"],
        },
    )
    assert out == {"status": 202, "body": "queued"}
    assert captured["url"] == "http://10.0.0.5/notify"
    assert json.loads(captured["kwargs"]["content"].decode("utf-8")) == {"event": "ping"}


@pytest.mark.asyncio
async def test_rejects_non_http_scheme(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(ValueError, match="http or https"):
        await handler(ctx, {"url": "ftp://host/x", "payload": {}})


@pytest.mark.asyncio
async def test_happy_path_public_host_stubbed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real public POST would need internet; stub the transport instead.
    ctx = _ctx(tmp_path)

    class _Resp:
        status_code = 200
        text = "ok"

    async def _fake_post(self: Any, url: str, **kwargs: Any) -> _Resp:
        return _Resp()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    # Use a public IP literal so the early SSRF check classifies it directly
    # (no DNS — the sandbox has no resolver) and lets it through.
    out = await handler(
        ctx,
        {
            "url": "https://93.184.216.34/services/abc",
            "payload": {"text": "hello"},
            "headers": {"X-Token": "secret"},
        },
    )
    assert out == {"status": 200, "body": "ok"}


@pytest.mark.asyncio
async def test_body_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path)

    class _Resp:
        status_code = 200
        text = "z" * (200 * 1024)

    async def _fake_post(self: Any, url: str, **kwargs: Any) -> _Resp:
        return _Resp()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    out = await handler(
        ctx,
        {"url": "https://93.184.216.34/x", "payload": {}},
    )
    assert out["status"] == 200
    assert out["body"].endswith("...[truncated]")
    assert len(out["body"]) < 200 * 1024
