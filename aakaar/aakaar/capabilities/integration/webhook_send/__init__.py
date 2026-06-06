"""cap.webhook_send — POST a JSON payload to an outbound webhook URL.

A thin, SSRF-guarded HTTP POST. The planner reaches for this when a flow
needs to notify an external system (a Slack/Teams incoming webhook, an
internal automation endpoint, a partner callback) by sending a JSON body.

Why a capability and not a raw HTTP action:
  - It pins the method to POST + JSON so the planner can't accidentally
    leak data over GET query strings.
  - It routes every request through the SSRF guard
    (`aakaar.core.net.ssrf`), so a model-chosen URL can't be tricked into
    hitting loopback, link-local, or cloud-metadata endpoints. Reaching a
    service on the local network requires the grant/DAG to opt that exact
    host in via `allow_hosts`.

This capability holds no credentials. If a webhook needs a bearer token
or signing secret, pass it in `headers` from an upstream node that pulled
it from the vault — webhook_send never fetches secrets itself, and never
logs header values or the payload body.

The response body is returned as text (decoded best-effort). Large bodies
are truncated for the run timeline; callers needing the full response
should not rely on this capability for data retrieval.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar.core.net.ssrf import SsrfBlocked, assert_host_allowed, build_async_client
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import CapabilityDefinition

logger = logging.getLogger(__name__)
CAP_REF = "cap.webhook_send"

_DEFAULT_TIMEOUT_S = 30.0
# Cap on how much of the response body we keep / return. The timeline UI
# does not need megabytes of webhook acknowledgements.
_MAX_BODY_CHARS = 64 * 1024


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(
        description=(
            "Absolute http/https URL to POST to. Resolved hosts that map to "
            "private/loopback/link-local addresses are blocked unless the host "
            "is listed in allow_hosts."
        )
    )
    payload: dict[str, Any] = Field(
        description="JSON-serializable object sent as the request body (Content-Type: application/json)."
    )
    headers: dict[str, str] | None = Field(
        default=None,
        description=(
            "Optional extra request headers (e.g. Authorization). Values are "
            "never logged. Content-Type defaults to application/json and may "
            "be overridden here."
        ),
    )
    allow_hosts: list[str] | None = Field(
        default=None,
        description=(
            "Exact hostnames permitted to resolve to private/internal "
            "addresses. Use this to reach a service on the local network; "
            "everything else private stays blocked."
        ),
    )
    timeout_s: float = Field(
        default=_DEFAULT_TIMEOUT_S,
        gt=0,
        le=300,
        description="Request timeout in seconds.",
    )


class _Outputs(BaseModel):
    status: int = Field(description="HTTP status code returned by the endpoint.")
    body: str = Field(description="Response body decoded as text (possibly truncated).")


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "POST a JSON payload to an outbound webhook URL through the SSRF "
        "guard. Returns the HTTP status and response body text. Holds no "
        "credentials; pass any auth token via headers. allow_hosts opts a "
        "specific internal host past the private-address block."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("integration", "http", "webhook", "notify"),
)


def build_request_kwargs(
    payload: dict[str, Any], headers: dict[str, str] | None
) -> dict[str, Any]:
    """Assemble the httpx request kwargs for the JSON POST.

    Pure helper (no I/O) so the body/header assembly can be unit-tested
    without a live server. We serialize the JSON ourselves and send it as
    ``content`` so the Content-Type header stays caller-overridable while
    still defaulting to application/json.
    """
    merged: dict[str, str] = {"Content-Type": "application/json"}
    if headers:
        # Caller-supplied headers win (case-insensitively in httpx, but we
        # merge plainly here — httpx normalizes on send).
        merged.update({str(k): str(v) for k, v in headers.items()})
    body = json.dumps(payload, default=str)
    return {"content": body.encode("utf-8"), "headers": merged}


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    url = inputs["url"]
    payload = inputs["payload"]
    headers = inputs.get("headers")
    allow_hosts = tuple(inputs.get("allow_hosts") or ())
    timeout = float(inputs.get("timeout_s", _DEFAULT_TIMEOUT_S))

    if not isinstance(payload, dict):
        raise ValueError("cap.webhook_send: `payload` must be a JSON object (dict)")

    # Parse the host for an early, clear SSRF rejection. The async transport
    # also re-checks at connect time (defense in depth / redirects), but
    # validating here gives a precise error before we open a client.
    import httpx

    parsed = httpx.URL(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"cap.webhook_send: url must be http or https, got {parsed.scheme!r}"
        )
    host = parsed.host
    # Raises SsrfBlocked if the target resolves to a non-public address and
    # is not allow-listed.
    assert_host_allowed(host, allow_hosts=allow_hosts)

    req_kwargs = build_request_kwargs(payload, headers)

    logger.info(
        "cap.webhook_send start run_id=%s host=%s payload_keys=%d allow_hosts=%d",
        ctx.run_id,
        host,
        len(payload),
        len(allow_hosts),
    )

    async with build_async_client(allow_hosts=allow_hosts, timeout=timeout) as client:
        try:
            resp = await client.post(url, **req_kwargs)
        except SsrfBlocked:
            # Surface the SSRF rejection unchanged so the orchestrator can
            # classify it; never wrap it into a generic transport error.
            raise

    body_text = resp.text or ""
    if len(body_text) > _MAX_BODY_CHARS:
        body_text = body_text[:_MAX_BODY_CHARS] + "...[truncated]"

    logger.info(
        "cap.webhook_send ok run_id=%s host=%s status=%d body_len=%d",
        ctx.run_id,
        host,
        resp.status_code,
        len(body_text),
    )
    return {"status": resp.status_code, "body": body_text}
