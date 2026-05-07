"""http.request — generic HTTP primitive.

Used as a fallback when no capability covers the target. Capabilities
should be preferred for anything authenticated; raw HTTP is for pure-public
calls or quick prototyping.
"""

from __future__ import annotations

from typing import Any

import httpx

from aakar.interpreter.activities.registry import ActivityRegistry
from aakar.interpreter.activities.types import ActivityContext


async def http_request(_ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    method = inputs["method"]
    url = inputs["url"]
    headers = inputs.get("headers") or {}
    body = inputs.get("body")
    timeout_ms = int(inputs.get("timeout_ms", 30000))

    async with httpx.AsyncClient(timeout=timeout_ms / 1000.0) as client:
        if isinstance(body, (dict, list)):
            response = await client.request(method, url, headers=headers, json=body)
        elif isinstance(body, str):
            response = await client.request(method, url, headers=headers, content=body)
        elif body is None:
            response = await client.request(method, url, headers=headers)
        else:
            response = await client.request(method, url, headers=headers, content=str(body))

    # Best-effort body decode: try JSON, fall back to text. Activities should
    # never raise on a non-2xx — that's the workflow author's call to make.
    parsed: Any
    try:
        parsed = response.json()
    except Exception:
        parsed = response.text

    return {
        "status": response.status_code,
        "headers": dict(response.headers),
        "body": parsed,
    }


def register_into(reg: ActivityRegistry) -> None:
    reg.register("http.request", http_request)
