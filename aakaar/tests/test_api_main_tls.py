"""api/main.py: OpenAI client construction honors AAKAAR_OPENAI_TLS_VERIFY,
and the module (the uvicorn entrypoint) builds with the flag both ways.

`aakaar.api.main` builds the real app at import time, so every test imports it
through `_import_main`, which pins a hermetic environment (tmp data dir, no
.env pickup, fake-LLM path) before the import runs.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from aakaar.core.config import Settings, load_settings


def _settings(**kw) -> Settings:
    return Settings(
        jwt_secret="x" * 48,
        openai_api_key="sk-test",
        **kw,
    )


def _import_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, tls_verify: str = "true"
):
    monkeypatch.chdir(tmp_path)  # keep load_dotenv away from the repo .env
    monkeypatch.setenv("AAKAAR_JWT_SECRET", "x" * 48)
    monkeypatch.setenv("AAKAAR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AAKAAR_BROWSER_POOL", "none")
    monkeypatch.setenv("AAKAAR_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("AAKAAR_OPENAI_TLS_VERIFY", tls_verify)
    # Force the fake-LLM path so the import never reaches OpenAI/HuggingFace.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("AAKAAR_OPENAI_BASE_URL", raising=False)
    sys.modules.pop("aakaar.api.main", None)
    return importlib.import_module("aakaar.api.main")


@pytest.fixture()
def main_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    module = _import_main(monkeypatch, tmp_path)
    try:
        yield module
    finally:
        sys.modules.pop("aakaar.api.main", None)
        module.app.state.deps.engine.dispose()


def test_build_openai_client_verifies_tls_by_default(
    main_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []

    class _SpyHttpx:
        def __init__(self, **kwargs) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(main_module.httpx, "Client", _SpyHttpx)
    client = main_module.build_openai_client(
        _settings(openai_base_url="https://llm.local:8443/v1", openai_tls_verify=True)
    )
    assert calls == []  # no custom http client: the SDK default verifies TLS
    assert str(client.base_url).startswith("https://llm.local:8443/v1")


def test_build_openai_client_disables_verify_when_asked(
    main_module, caplog: pytest.LogCaptureFixture
) -> None:
    client = main_module.build_openai_client(
        _settings(openai_base_url="https://llm.local:8443/v1", openai_tls_verify=False)
    )
    # The custom httpx client must carry verify=False end-to-end.
    transport_pool = client._client._transport._pool  # noqa: SLF001 - introspection
    assert transport_pool._ssl_context.verify_mode.name == "CERT_NONE"
    assert any("DISABLED" in r.message for r in caplog.records)


def test_build_openai_client_default_endpoint_ignores_flag(main_module) -> None:
    client = main_module.build_openai_client(_settings(openai_tls_verify=False))
    assert "api.openai.com" in str(client.base_url)


def test_load_settings_parses_tls_verify_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env pickup
    monkeypatch.setenv("AAKAAR_JWT_SECRET", "x" * 48)
    monkeypatch.setenv("AAKAAR_OPENAI_TLS_VERIFY", "false")
    assert load_settings().openai_tls_verify is False
    monkeypatch.setenv("AAKAAR_OPENAI_TLS_VERIFY", "true")
    assert load_settings().openai_tls_verify is True
    monkeypatch.delenv("AAKAAR_OPENAI_TLS_VERIFY")
    assert load_settings().openai_tls_verify is True


@pytest.mark.parametrize("tls_verify", ["true", "false"])
def test_main_module_builds_with_flag_both_ways(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tls_verify: str
) -> None:
    module = _import_main(monkeypatch, tmp_path, tls_verify=tls_verify)
    try:
        assert module.app is not None
        assert module.app.title == "Aakaar"
    finally:
        sys.modules.pop("aakaar.api.main", None)
        module.app.state.deps.engine.dispose()
