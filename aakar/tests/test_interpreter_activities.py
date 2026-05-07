"""Built-in activity tests — http, file, storage."""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest

from aakar.interpreter.activities import build_default_activities
from aakar.interpreter.activities.types import ActivityContext
from aakar.shared.registry import build_default_registry
from aakar.storage import LocalFsObjectStore
from aakar.storage.object_store import make_uri
from aakar.vault import LocalVault


def _actx(tmp_path: Path) -> ActivityContext:
    return ActivityContext(
        tenant_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        registry=build_default_registry(),
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=LocalVault(tmp_path / "vault"),
    )


@pytest.mark.asyncio
async def test_file_parse_csv_round_trip(tmp_path: Path) -> None:
    activities = build_default_activities()
    actx = _actx(tmp_path)
    csv_text = "name,age\nAlice,30\nBob,42\n"
    obj = actx.object_store.put(str(actx.tenant_id), "x.csv", csv_text.encode())
    parser = activities.get("file.parse_csv")
    assert parser is not None
    out = await parser(actx, {"file_uri": obj.uri})
    assert out == {"rows": [{"name": "Alice", "age": "30"}, {"name": "Bob", "age": "42"}]}


@pytest.mark.asyncio
async def test_file_write_csv_round_trip(tmp_path: Path) -> None:
    activities = build_default_activities()
    actx = _actx(tmp_path)
    target = make_uri(str(actx.tenant_id), "out.csv")
    rows = [{"x": 1, "y": "a"}, {"x": 2, "y": "b"}]
    writer = activities.get("file.write_csv")
    assert writer is not None
    out = await writer(actx, {"file_uri": target, "rows": rows})
    assert out == {"file_uri": target}

    raw = actx.object_store.get(target).decode()
    assert "x,y" in raw and "1,a" in raw and "2,b" in raw


@pytest.mark.asyncio
async def test_http_request(monkeypatch, tmp_path: Path) -> None:
    activities = build_default_activities()
    actx = _actx(tmp_path)

    captured: dict = {}

    def fake_handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(fake_handler)

    # Patch the AsyncClient inside the activity to use the mock transport.
    real_client_cls = httpx.AsyncClient

    def patched_client_factory(**kwargs):
        kwargs.setdefault("transport", transport)
        return real_client_cls(**kwargs)

    monkeypatch.setattr("aakar.interpreter.activities.http.httpx.AsyncClient", patched_client_factory)

    handler = activities.get("http.request")
    assert handler is not None
    out = await handler(actx, {"method": "GET", "url": "https://x.test/path"})
    assert out["status"] == 200
    assert out["body"] == {"ok": True}
    assert captured == {"url": "https://x.test/path", "method": "GET"}


@pytest.mark.asyncio
async def test_storage_put_then_get(tmp_path: Path) -> None:
    activities = build_default_activities()
    actx = _actx(tmp_path)

    src = tmp_path / "source.bin"
    src.write_bytes(b"payload")

    put = activities.get("storage.put")
    get = activities.get("storage.get")
    assert put is not None and get is not None

    put_out = await put(actx, {"key": "p/payload.bin", "source_file_uri": f"file://{src}"})
    uri = put_out["uri"]
    assert uri.startswith("aakar://t/")

    get_out = await get(actx, {"uri": uri})
    local_path = Path(get_out["file_uri"].removeprefix("file://"))
    assert local_path.read_bytes() == b"payload"


@pytest.mark.asyncio
async def test_storage_get_rejects_cross_tenant_uri(tmp_path: Path) -> None:
    activities = build_default_activities()
    actx = _actx(tmp_path)
    foreign_uri = make_uri("other-tenant", "k")
    handler = activities.get("storage.get")
    assert handler is not None
    with pytest.raises(PermissionError):
        await handler(actx, {"uri": foreign_uri})
