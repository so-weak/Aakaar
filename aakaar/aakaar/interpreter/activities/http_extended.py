"""http.graphql / http.soap — protocol-specific HTTP primitives.

Like `http.request`, these are fallbacks for public/quick-prototype calls;
anything authenticated should go through a capability. Unlike the bare
`http.request`, every outbound call here goes through the SSRF-safe client
(`aakaar.core.net.ssrf.build_async_client`) because GraphQL/SOAP endpoints
are exactly the kind of arbitrary-URL targets that must not be tricked into
hitting internal services or cloud metadata endpoints.

Activities never raise on a non-2xx response — surfacing status to the
workflow author is their call to make. They DO raise `SsrfBlocked` when the
target resolves to a non-public address (unless the grant allows it).
"""

from __future__ import annotations

import logging
from typing import Any

from aakaar.core.net.ssrf import build_async_client
from aakaar.interpreter.activities.registry import ActivityRegistry
from aakaar.interpreter.activities.types import ActivityContext

logger = logging.getLogger(__name__)


def _ssrf_kwargs(inputs: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    """Common SSRF client knobs sourced from the node inputs/grant."""
    return {
        "allow_hosts": tuple(inputs.get("allow_hosts") or ()),
        "allow_private": bool(inputs.get("allow_private", False)),
        "timeout": timeout_ms / 1000.0,
    }


async def graphql(_ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """POST a GraphQL query and return its data/errors envelope.

    inputs:
      url (required)        : GraphQL endpoint
      query (required)      : GraphQL query/mutation string
      variables (optional)  : dict of query variables
      operation_name (opt)  : selects an operation in a multi-op document
      headers (optional)    : extra request headers
      timeout_ms (optional, default 30000)
      allow_hosts / allow_private : SSRF allowlist knobs
    """
    url = inputs["url"]
    query = inputs["query"]
    variables = inputs.get("variables") or {}
    operation_name = inputs.get("operation_name")
    headers = dict(inputs.get("headers") or {})
    headers.setdefault("Content-Type", "application/json")
    timeout_ms = int(inputs.get("timeout_ms", 30000))

    payload: dict[str, Any] = {"query": query, "variables": variables}
    if operation_name:
        payload["operationName"] = operation_name

    async with build_async_client(**_ssrf_kwargs(inputs, timeout_ms)) as client:
        response = await client.post(url, headers=headers, json=payload)

    body: Any
    try:
        body = response.json()
    except Exception:
        body = None

    data = body.get("data") if isinstance(body, dict) else None
    errors = body.get("errors") if isinstance(body, dict) else None

    logger.debug(
        "http.graphql url=%s status=%d has_errors=%s",
        url,
        response.status_code,
        bool(errors),
    )
    return {
        "status": response.status_code,
        "data": data,
        "errors": errors,
        "headers": dict(response.headers),
    }


async def soap(_ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """POST a SOAP envelope and return the raw response text + status.

    inputs:
      url (required)        : SOAP endpoint
      envelope (required)   : the full XML SOAP envelope (string)
      soap_action (optional): value for the SOAPAction header
      headers (optional)    : extra request headers
      timeout_ms (optional, default 30000)
      allow_hosts / allow_private : SSRF allowlist knobs
    """
    url = inputs["url"]
    envelope = inputs["envelope"]
    soap_action = inputs.get("soap_action")
    headers = dict(inputs.get("headers") or {})
    headers.setdefault("Content-Type", "text/xml; charset=utf-8")
    if soap_action is not None:
        # SOAP 1.1 wraps the action in quotes; pass through as given otherwise.
        headers.setdefault("SOAPAction", str(soap_action))

    timeout_ms = int(inputs.get("timeout_ms", 30000))

    if not isinstance(envelope, str):
        raise ValueError("soap 'envelope' must be a string XML document")

    async with build_async_client(**_ssrf_kwargs(inputs, timeout_ms)) as client:
        response = await client.post(url, headers=headers, content=envelope.encode("utf-8"))

    logger.debug("http.soap url=%s action=%r status=%d", url, soap_action, response.status_code)
    return {
        "status": response.status_code,
        "body": response.text,
        "headers": dict(response.headers),
    }


def register_into(reg: ActivityRegistry) -> None:
    reg.register("http.graphql", graphql)
    reg.register("http.soap", soap)
