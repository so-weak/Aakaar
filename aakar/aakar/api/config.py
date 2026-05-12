"""Application configuration.

Read from environment variables. Tests construct Settings directly with
explicit values rather than going through env, so test isolation doesn't
depend on how the host environment is configured.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(slots=True)
class Settings:
    db_url: str = "sqlite:///./data/aakar.sqlite"
    data_dir: Path = field(default_factory=lambda: Path("./data"))
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 60 * 24
    superuser_email: str | None = None
    superuser_password: str | None = None
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    llm_model: str = "gpt-4.1-mini"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embeddings_dim: int = 16  # only used by FakeEmbeddingsClient; BGE derives its own dim
    browser_pool: str = "playwright"  # "playwright" | "none"
    browser_headless: bool = True
    live_screenshots: bool = True
    """Capture a per-node screenshot of the active browser session and
    emit a `live_screen` event the UI streams into the run view. Disable
    via AAKAR_LIVE_SCREENSHOTS=false to save object-store space if the
    live panel isn't needed."""
    download_mirror_dir: Path | None = None
    """When set, cap.file_download writes an extra copy of every
    downloaded file into this directory on the worker host (in addition
    to managed object storage). Intended for dev use — point it at
    `~/Downloads` so files land where a browser would put them. Leave
    unset in deployed environments; the object store remains the
    canonical location. Path is expanded with `~`/$VAR before use."""
    cors_allow_origins: list[str] = field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )


def load_settings() -> Settings:
    """Build a Settings instance from environment variables.

    Required: AAKAR_JWT_SECRET (a long random string).
    Everything else has a reasonable default.
    """
    load_dotenv(Path.cwd() / ".env")

    # The OpenAI SDK reads OPENAI_BASE_URL straight from the environment.
    # An empty value (e.g. `OPENAI_BASE_URL=` in .env) is treated as a real
    # base URL of "" and breaks every request. Drop it so the SDK falls
    # back to its default endpoint.
    if os.environ.get("OPENAI_BASE_URL", "").strip() == "":
        os.environ.pop("OPENAI_BASE_URL", None)

    jwt_secret = os.environ.get("AAKAR_JWT_SECRET", "")
    if not jwt_secret:
        # Refuse to silently start without a secret — auth would mint
        # forgeable tokens. The dev convenience is to set this once via
        # `python -c 'import secrets; print(secrets.token_urlsafe(48))'`.
        raise RuntimeError(
            "AAKAR_JWT_SECRET is not set; refusing to start. "
            "Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
        )

    data_dir = Path(os.environ.get("AAKAR_DATA_DIR", "./data"))
    raw_mirror = os.environ.get("AAKAR_DOWNLOAD_MIRROR_DIR", "").strip()
    download_mirror_dir = (
        Path(os.path.expandvars(raw_mirror)).expanduser() if raw_mirror else None
    )
    raw_origins = os.environ.get(
        "AAKAR_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    )
    cors_origins = [o.strip() for o in raw_origins.split(",") if o.strip()]

    return Settings(
        db_url=os.environ.get("AAKAR_DB_URL", f"sqlite:///{data_dir/'aakar.sqlite'}"),
        data_dir=data_dir,
        jwt_secret=jwt_secret,
        jwt_algorithm=os.environ.get("AAKAR_JWT_ALG", "HS256"),
        access_token_ttl_minutes=int(os.environ.get("AAKAR_ACCESS_TTL_MIN", "1440")),
        superuser_email=os.environ.get("AAKAR_SUPERUSER_EMAIL"),
        superuser_password=os.environ.get("AAKAR_SUPERUSER_PASSWORD"),
        openai_api_key=os.environ.get("OPENAI_API_KEY") or None,
        openai_base_url=(
            os.environ.get("OPENAI_BASE_URL") or os.environ.get("AAKAR_OPENAI_BASE_URL") or None
        ),
        llm_model=os.environ.get("AAKAR_LLM_MODEL", "gpt-4.1-mini"),
        embedding_model=os.environ.get("AAKAR_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"),
        embeddings_dim=int(os.environ.get("AAKAR_EMBEDDINGS_DIM", "16")),
        browser_pool=os.environ.get("AAKAR_BROWSER_POOL", "playwright").lower(),
        browser_headless=os.environ.get("AAKAR_BROWSER_HEADLESS", "true").lower()
        not in ("0", "false", "no"),
        live_screenshots=os.environ.get("AAKAR_LIVE_SCREENSHOTS", "true").lower()
        not in ("0", "false", "no"),
        download_mirror_dir=download_mirror_dir,
        cors_allow_origins=cors_origins,
    )


def random_secret(nbytes: int = 48) -> str:
    """Convenience for tests / CLI: generate a fresh JWT secret."""
    return secrets.token_urlsafe(nbytes)
