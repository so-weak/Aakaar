"""cap.file_download — download a file through an authenticated browser session.

Composes `browser.wait_for` (so we don't race the page) and the underlying
download mechanism, and persists the result to managed storage. Returns
the storage URI so downstream nodes can reference the file by `${ref.uri}`.

Two ways to specify what to download (exactly one must be supplied):
  - `trigger_selector` — a CSS selector for a link or button to click.
    Use this for portals where the report URL is dynamic or session-bound.
  - `url` — a direct download URL the session is authenticated for.

The capability does NOT log in. It expects a `session` produced by an
upstream node (typically `cap.web_login`).
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aakar.interpreter.activities.browser import _get_session
from aakar.interpreter.activities.types import ActivityContext
from aakar.shared.registry import CapabilityDefinition


CAP_REF = "cap.file_download"


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str = Field(
        description="Authenticated browser session handle, e.g. ${login.session}."
    )
    trigger_selector: str | None = Field(
        default=None,
        description=(
            "CSS selector for a link/button that initiates the download when "
            "clicked. Mutually exclusive with `url`."
        ),
    )
    url: str | None = Field(
        default=None,
        description=(
            "Direct download URL the authenticated session can fetch. "
            "Mutually exclusive with `trigger_selector`."
        ),
    )
    wait_for: str | None = Field(
        default=None,
        description=(
            "Optional CSS selector to wait for before triggering the download "
            "(e.g. ensure the report list has rendered)."
        ),
    )
    timeout_ms: int = Field(
        default=15000, ge=1000, le=120000, description="Selector wait timeout."
    )

    @model_validator(mode="after")
    def _check_one_of(self) -> "_Inputs":
        if bool(self.trigger_selector) == bool(self.url):
            raise ValueError(
                "exactly one of `trigger_selector` or `url` must be provided"
            )
        return self


class _Outputs(BaseModel):
    uri: str = Field(description="Managed-storage URI of the downloaded file.")
    filename: str = Field(description="Original filename reported by the browser/server.")


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Download a file through an authenticated browser session and store "
        "it in managed storage. Returns the storage URI."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("download", "browser"),
)


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = _get_session(ctx, inputs["session"])

    wait_selector = inputs.get("wait_for")
    if wait_selector:
        await sess.wait_for(wait_selector, timeout_ms=int(inputs.get("timeout_ms", 15000)))

    file = await sess.download(
        trigger_selector=inputs.get("trigger_selector"),
        url=inputs.get("url"),
    )
    key = f"runs/{ctx.run_id}/downloads/{uuid.uuid4().hex}_{file.filename}"
    obj = ctx.object_store.put(str(ctx.tenant_id), key, file.content)
    return {"uri": obj.uri, "filename": file.filename}
