"""cap.api_call — authenticated outbound HTTP request.

A generic, SSRF-guarded HTTP client capability. Unlike the raw
``http.request`` action primitive, this capability can attach credentials
from the tenant vault by ``account_alias`` and lets a grant pin an
``allow_hosts`` allowlist so calls to private/LAN services are permitted
where the bare SsrfGuard would otherwise block them.

Auth is inferred from whichever secrets the grant stores (the
capability's ``secrets`` are all optional — an unauthenticated call needs
no grant at all and may omit ``account_alias``):

  token            -> Authorization: Bearer <token>
  api_key          -> a header carrying the key. The header name comes
                      from the grant's ``input_defaults.api_key_header``
                      (default ``X-API-Key``). If that default is set to
                      ``Authorization`` the key is sent verbatim, so a
                      grant can store a pre-formatted "Bearer ..." or
                      "Token ..." value when a service wants that shape.
  username+password-> HTTP Basic (base64 of "user:pass").

If more than one of these is present the precedence is
bearer > api_key > basic; only the first match is applied. Explicit
``headers`` on the node take precedence over the auth header on a
case-insensitive key clash (the caller asked for it; we don't clobber).

``allow_hosts`` may be supplied on the node and/or on the grant's
``input_defaults``; the two are unioned. Hosts on the list are allowed to
resolve to private addresses (see aakaar.core.net.ssrf).

Output: ``{status, headers, body}``. The body is JSON-decoded when
possible, else returned as text. Non-2xx responses are returned, not
raised — branching on status is the workflow author's call. Secret values
are never logged or echoed in the output.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar.core.net.ssrf import build_async_client
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.interpreter.credentials import fetch_credentials
from aakaar.shared.registry import CapabilityDefinition, SecretSpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.api_call"

_DEFAULT_TIMEOUT_S = 30.0
_DEFAULT_API_KEY_HEADER = "X-API-Key"
_ALLOWED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: str = Field(
        description="HTTP method, e.g. GET / POST / PUT / PATCH / DELETE.",
    )
    url: str = Field(description="Absolute URL to request (http or https).")
    account_alias: str | None = Field(
        default=None,
        description=(
            "Optional credential set to authenticate with. When null the "
            "request is sent unauthenticated and no grant is required."
        ),
    )
    headers: dict[str, str] | None = Field(
        default=None,
        description=(
            "Optional request headers. These take precedence over any "
            "auth header on a case-insensitive name clash."
        ),
    )
    query: dict[str, str] | None = Field(
        default=None,
        description="Optional query-string parameters appended to the URL.",
    )
    json_body: Any = Field(
        default=None,
        description=(
            "Optional JSON-serialisable request body. Sent as application/json. "
            "Leave null for bodyless requests."
        ),
    )
    allow_hosts: list[str] | None = Field(
        default=None,
        description=(
            "Optional exact hostnames permitted to resolve to private/LAN "
            "addresses. Unioned with the grant's input_defaults.allow_hosts."
        ),
    )
    timeout_s: float = Field(
        default=_DEFAULT_TIMEOUT_S,
        gt=0,
        le=300,
        description="Per-request timeout in seconds.",
    )


class _Outputs(BaseModel):
    status: int = Field(description="HTTP status code of the response.")
    headers: dict[str, str] = Field(description="Response headers.")
    body: Any = Field(description="Response body: JSON-decoded when possible, else text.")


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Make an SSRF-guarded HTTP request, optionally authenticated with "
        "stored credentials (bearer token, API key, or HTTP Basic depending "
        "on what the grant holds). Supports query params, a JSON body, and a "
        "per-host private-address allowlist. Returns status, headers, and body."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(
        SecretSpec(
            name="token",
            description=(
                "Bearer token. When present, sent as 'Authorization: Bearer "
                "<token>'. Optional."
            ),
        ),
        SecretSpec(
            name="api_key",
            description=(
                "API key. When present (and no token), sent in the header "
                "named by input_defaults.api_key_header (default X-API-Key). "
                "Optional."
            ),
        ),
        SecretSpec(
            name="username",
            description="Username for HTTP Basic auth. Pair with `password`. Optional.",
        ),
        SecretSpec(
            name="password",
            description="Password for HTTP Basic auth. Pair with `username`. Optional.",
        ),
    ),
    tags=("http", "integration", "api"),
)


def build_auth_headers(
    creds: dict[str, str], *, api_key_header: str = _DEFAULT_API_KEY_HEADER
) -> dict[str, str]:
    """Map vault secrets to the auth header(s) they imply.

    Precedence is bearer > api_key > basic; only the first match wins so we
    never send conflicting Authorization headers. Returns an empty dict when
    no recognised secret is present (an unauthenticated request). Never logs
    or returns the secret values to the caller beyond the header itself.
    """
    token = (creds.get("token") or "").strip()
    if token:
        return {"Authorization": f"Bearer {token}"}

    api_key = (creds.get("api_key") or "").strip()
    if api_key:
        name = (api_key_header or _DEFAULT_API_KEY_HEADER).strip() or _DEFAULT_API_KEY_HEADER
        return {name: api_key}

    username = creds.get("username") or ""
    password = creds.get("password") or ""
    if username or password:
        raw = f"{username}:{password}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}

    return {}


def merge_headers(
    base: dict[str, str], override: dict[str, str] | None
) -> dict[str, str]:
    """Merge `base` (auth) with caller `override` headers.

    Caller headers win on a case-insensitive key clash. Returns a fresh dict;
    inputs are not mutated.
    """
    merged = dict(base)
    if override:
        lowered = {k.lower(): k for k in merged}
        for key, value in override.items():
            existing = lowered.get(key.lower())
            if existing is not None and existing != key:
                del merged[existing]
            merged[key] = value
    return merged


def _normalize_method(method: str) -> str:
    m = (method or "").strip().upper()
    if m not in _ALLOWED_METHODS:
        raise ValueError(
            f"cap.api_call: unsupported method {method!r}; "
            f"expected one of {', '.join(_ALLOWED_METHODS)}"
        )
    return m


def _grant_defaults(ctx: ActivityContext, alias: str) -> dict[str, Any]:
    return (
        (ctx.granted_capabilities.get(CAP_REF) or {}).get(alias) or {}
    ).get("input_defaults") or {}


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    method = _normalize_method(inputs["method"])
    url = inputs["url"]
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"cap.api_call: url must be an absolute http(s) URL, got {url!r}")

    alias = inputs.get("account_alias")
    query = inputs.get("query")
    json_body = inputs.get("json_body")
    timeout = float(inputs.get("timeout_s", _DEFAULT_TIMEOUT_S))

    auth_headers: dict[str, str] = {}
    allow_hosts: set[str] = set(inputs.get("allow_hosts") or [])

    if alias:
        creds = fetch_credentials(ctx, capability_ref=CAP_REF, account_alias=alias)
        defaults = _grant_defaults(ctx, alias)
        api_key_header = str(defaults.get("api_key_header") or _DEFAULT_API_KEY_HEADER)
        auth_headers = build_auth_headers(creds, api_key_header=api_key_header)
        allow_hosts.update(defaults.get("allow_hosts") or [])

    request_headers = merge_headers(auth_headers, inputs.get("headers"))

    logger.info(
        "cap.api_call start run_id=%s method=%s url=%s alias=%s authed=%s "
        "allow_hosts=%d",
        ctx.run_id,
        method,
        url,
        alias,
        bool(auth_headers),
        len(allow_hosts),
    )

    client = build_async_client(allow_hosts=sorted(allow_hosts), timeout=timeout)
    try:
        kwargs: dict[str, Any] = {"headers": request_headers}
        if query:
            kwargs["params"] = query
        if json_body is not None:
            kwargs["json"] = json_body
        response = await client.request(method, url, **kwargs)
    finally:
        await client.aclose()

    parsed: Any
    try:
        parsed = response.json()
    except Exception:
        parsed = response.text

    logger.info(
        "cap.api_call ok run_id=%s method=%s url=%s status=%d",
        ctx.run_id,
        method,
        url,
        response.status_code,
    )
    return {
        "status": response.status_code,
        "headers": dict(response.headers),
        "body": parsed,
    }
