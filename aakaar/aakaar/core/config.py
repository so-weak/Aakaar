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
    db_url: str = "sqlite:///./data/aakaar.sqlite"
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
    embeddings_offline: bool = False
    """When true, the BGE embedder loads only from the local Hugging Face
    cache and never reaches the hub (sets HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE
    and local_files_only=True). Required on the airgapped target, where the
    model is pre-staged into AAKAAR_DATA_DIR/hf_cache. Leave false on a
    networked machine so the first run can populate the cache."""
    browser_pool: str = "playwright"  # "playwright" | "none"
    browser_headless: bool = True
    live_screenshots: bool = True
    """Capture a per-node screenshot of the active browser session and
    emit a `live_screen` event the UI streams into the run view. Disable
    via AAKAAR_LIVE_SCREENSHOTS=false to save object-store space if the
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
    metrics_enabled: bool = True
    """Expose a Prometheus /metrics endpoint (scraped locally; no external
    egress). Disable via AAKAAR_METRICS_ENABLED=false."""
    rate_limit_enabled: bool = True
    rate_limit_per_min: int = 240
    rate_limit_auth_per_min: int = 20
    scheduler_enabled: bool = True
    """Run the in-process workflow scheduler (cron + one-off). Disabled in
    tests so the background poll loop doesn't fire."""
    scheduler_tick_seconds: float = 5.0
    remote_exec_enabled: bool = True
    """Allow nodes to be placed on remote agents (the /ws/agents endpoint +
    RemoteDispatcher). Inert when no agents are enrolled."""
    remote_task_timeout_seconds: float = 300.0

    # ---- JWT signing (HS256 default; RS256 for production) ----------------
    jwt_issuer: str | None = None
    """`iss` claim stamped on minted tokens. Optional under HS256; recommended
    under RS256 so downstream verifiers can pin the issuer."""
    jwt_audience: str = "aakaar-api"
    """`aud` claim for normal access tokens. The MFA step-up ticket uses a
    different audience so it can never be replayed as an access token."""
    jwt_key_dir: Path | None = None
    """Directory of RSA signing keys for RS256: `<kid>.pem` (PKCS8 private),
    optional `<kid>.pem.pub` (public, derived if absent), and an `active` file
    naming the current signing kid. Required when jwt_algorithm starts with
    RS/ES/PS. Publishing every public key (see /auth/.well-known/jwks.json)
    lets tokens signed by an older kid keep validating across a rotation."""
    jwt_bootstrap_keys: bool = False
    """Dev convenience: when jwt_key_dir is empty, generate an RSA keypair on
    first start (written unencrypted). NEVER enable in production."""

    # ---- MFA (TOTP) -------------------------------------------------------
    mfa_issuer: str = "Aakaar"
    """Issuer label shown in the authenticator app (otpauth:// provisioning)."""
    mfa_encryption_key: str | None = None
    """Optional Fernet key (urlsafe-base64, 32 bytes) used to encrypt TOTP
    secrets at rest. Unset = secrets stored verbatim (acceptable for dev /
    SQLite; set this in production). Generate with:
    `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`."""

    # ---- OIDC / SSO -------------------------------------------------------
    oidc_enabled: bool = False
    oidc_issuer: str | None = None
    """Base issuer URL; discovery reads `{issuer}/.well-known/openid-configuration`."""
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_redirect_uri: str | None = None
    """The absolute URL of our /auth/oidc/callback as registered with the IdP."""
    oidc_frontend_callback_path: str = "/auth/callback"
    """SPA route that receives the minted token in the URL fragment."""
    oidc_default_tenant_slug: str | None = None
    """Tenant new OIDC users are provisioned into when the login request does
    not carry an explicit `tenant` hint. Unset = first-login must pass one."""
    oidc_link_by_verified_email: bool = False
    """When true, an OIDC login whose id_token has `email_verified=true` links
    to an existing local user with that email instead of provisioning a new
    one. Off by default — account linking is a deliberate policy choice."""

    # ---- Row-Level Security ----------------------------------------------
    rls_strict: bool = False
    """When true, a DB session with neither a tenant nor a system scope active
    sets the `app.tenant_id` GUC to '' (deny-all) — fail-closed. Default false
    maps the no-scope case to the 'system' marker (allow-all) for a
    backward-compatible rollout. RLS only actually enforces when the app
    connects as a non-superuser, non-owner Postgres role (see
    extras/rls/setup_app_role.sql); on SQLite it is a no-op."""


def load_settings() -> Settings:
    """Build a Settings instance from environment variables.

    Required: AAKAAR_JWT_SECRET (a long random string).
    Everything else has a reasonable default.
    """
    load_dotenv(Path.cwd() / ".env")

    # The OpenAI SDK reads OPENAI_BASE_URL straight from the environment.
    # An empty value (e.g. `OPENAI_BASE_URL=` in .env) is treated as a real
    # base URL of "" and breaks every request. Drop it so the SDK falls
    # back to its default endpoint.
    if os.environ.get("OPENAI_BASE_URL", "").strip() == "":
        os.environ.pop("OPENAI_BASE_URL", None)

    jwt_secret = os.environ.get("AAKAAR_JWT_SECRET", "")
    if not jwt_secret:
        # Refuse to silently start without a secret — auth would mint
        # forgeable tokens. The dev convenience is to set this once via
        # `python -c 'import secrets; print(secrets.token_urlsafe(48))'`.
        raise RuntimeError(
            "AAKAAR_JWT_SECRET is not set; refusing to start. "
            "Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
        )

    data_dir = Path(os.environ.get("AAKAAR_DATA_DIR", "./data"))
    raw_mirror = os.environ.get("AAKAAR_DOWNLOAD_MIRROR_DIR", "").strip()
    download_mirror_dir = (
        Path(os.path.expandvars(raw_mirror)).expanduser() if raw_mirror else None
    )
    raw_origins = os.environ.get(
        "AAKAAR_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    )
    cors_origins = [o.strip() for o in raw_origins.split(",") if o.strip()]

    def _bool(name: str, default: str = "false") -> bool:
        return os.environ.get(name, default).lower() not in ("0", "false", "no", "")

    raw_key_dir = os.environ.get("AAKAAR_JWT_KEY_DIR", "").strip()
    jwt_key_dir = Path(raw_key_dir).expanduser() if raw_key_dir else None

    return Settings(
        db_url=os.environ.get("AAKAAR_DB_URL", f"sqlite:///{data_dir/'aakaar.sqlite'}"),
        data_dir=data_dir,
        jwt_secret=jwt_secret,
        jwt_algorithm=os.environ.get("AAKAAR_JWT_ALG", "HS256"),
        access_token_ttl_minutes=int(os.environ.get("AAKAAR_ACCESS_TTL_MIN", "1440")),
        superuser_email=os.environ.get("AAKAAR_SUPERUSER_EMAIL"),
        superuser_password=os.environ.get("AAKAAR_SUPERUSER_PASSWORD"),
        openai_api_key=os.environ.get("OPENAI_API_KEY") or None,
        openai_base_url=(
            os.environ.get("OPENAI_BASE_URL") or os.environ.get("AAKAAR_OPENAI_BASE_URL") or None
        ),
        llm_model=os.environ.get("AAKAAR_LLM_MODEL", "gpt-4.1-mini"),
        embedding_model=os.environ.get("AAKAAR_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"),
        embeddings_dim=int(os.environ.get("AAKAAR_EMBEDDINGS_DIM", "16")),
        embeddings_offline=os.environ.get("AAKAAR_HF_OFFLINE", "false").lower()
        in ("1", "true", "yes"),
        browser_pool=os.environ.get("AAKAAR_BROWSER_POOL", "playwright").lower(),
        browser_headless=os.environ.get("AAKAAR_BROWSER_HEADLESS", "true").lower()
        not in ("0", "false", "no"),
        live_screenshots=os.environ.get("AAKAAR_LIVE_SCREENSHOTS", "true").lower()
        not in ("0", "false", "no"),
        download_mirror_dir=download_mirror_dir,
        cors_allow_origins=cors_origins,
        metrics_enabled=os.environ.get("AAKAAR_METRICS_ENABLED", "true").lower()
        not in ("0", "false", "no"),
        rate_limit_enabled=os.environ.get("AAKAAR_RATE_LIMIT_ENABLED", "true").lower()
        not in ("0", "false", "no"),
        rate_limit_per_min=int(os.environ.get("AAKAAR_RATE_LIMIT_PER_MIN", "240")),
        rate_limit_auth_per_min=int(
            os.environ.get("AAKAAR_RATE_LIMIT_AUTH_PER_MIN", "20")
        ),
        scheduler_enabled=os.environ.get("AAKAAR_SCHEDULER_ENABLED", "true").lower()
        not in ("0", "false", "no"),
        scheduler_tick_seconds=float(
            os.environ.get("AAKAAR_SCHEDULER_TICK_SECONDS", "5")
        ),
        remote_exec_enabled=os.environ.get("AAKAAR_REMOTE_EXEC_ENABLED", "true").lower()
        not in ("0", "false", "no"),
        remote_task_timeout_seconds=float(
            os.environ.get("AAKAAR_REMOTE_TASK_TIMEOUT_SECONDS", "300")
        ),
        jwt_issuer=os.environ.get("AAKAAR_JWT_ISSUER") or None,
        jwt_audience=os.environ.get("AAKAAR_JWT_AUDIENCE", "aakaar-api"),
        jwt_key_dir=jwt_key_dir,
        jwt_bootstrap_keys=_bool("AAKAAR_JWT_BOOTSTRAP_KEYS"),
        mfa_issuer=os.environ.get("AAKAAR_MFA_ISSUER", "Aakaar"),
        mfa_encryption_key=os.environ.get("AAKAAR_MFA_ENCRYPTION_KEY") or None,
        oidc_enabled=_bool("AAKAAR_OIDC_ENABLED"),
        oidc_issuer=os.environ.get("AAKAAR_OIDC_ISSUER") or None,
        oidc_client_id=os.environ.get("AAKAAR_OIDC_CLIENT_ID") or None,
        oidc_client_secret=os.environ.get("AAKAAR_OIDC_CLIENT_SECRET") or None,
        oidc_redirect_uri=os.environ.get("AAKAAR_OIDC_REDIRECT_URI") or None,
        oidc_frontend_callback_path=os.environ.get(
            "AAKAAR_OIDC_FRONTEND_CALLBACK_PATH", "/auth/callback"
        ),
        oidc_default_tenant_slug=os.environ.get("AAKAAR_OIDC_DEFAULT_TENANT_SLUG") or None,
        oidc_link_by_verified_email=_bool("AAKAAR_OIDC_LINK_BY_VERIFIED_EMAIL"),
        rls_strict=_bool("AAKAAR_RLS_STRICT"),
    )


def random_secret(nbytes: int = 48) -> str:
    """Convenience for tests / CLI: generate a fresh JWT secret."""
    return secrets.token_urlsafe(nbytes)
