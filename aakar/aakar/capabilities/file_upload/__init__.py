"""cap.file_upload — attach a managed-storage file to a file input.

Materializes a file from the tenant's object store, hands it to the
browser session via `<input type=file>`, optionally clicks a submit
control, and (optionally) waits for a confirmation selector so the run
doesn't continue before the upload has actually been accepted.

The capability does NOT log in. It expects a `session` produced by an
upstream node (typically `cap.web_login`).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakar.interpreter.activities.browser import _get_session
from aakar.interpreter.activities.types import ActivityContext
from aakar.shared.registry import CapabilityDefinition


CAP_REF = "cap.file_upload"


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str = Field(
        description="Authenticated browser session handle, e.g. ${login.session}."
    )
    file_uri: str = Field(
        description=(
            "Managed-storage URI of the file to upload (`aakar://...`). "
            "Use upstream `${node.uri}` references; do not embed literal paths."
        ),
    )
    file_input_selector: str = Field(
        description="CSS selector for the <input type='file'> element."
    )
    submit_selector: str | None = Field(
        default=None,
        description=(
            "Optional CSS selector for a submit/upload button to click after "
            "the file is attached. Omit if the form auto-submits on selection."
        ),
    )
    success_selector: str | None = Field(
        default=None,
        description=(
            "Optional CSS selector that proves the upload was accepted "
            "(e.g. a success banner). The handler waits for it after submit."
        ),
    )
    timeout_ms: int = Field(
        default=30000, ge=1000, le=300000, description="Selector wait timeout."
    )


class _Outputs(BaseModel):
    file_uri: str = Field(description="The same file URI that was uploaded (echo).")


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Upload a file from managed storage through an authenticated browser "
        "session. Materializes the file locally, attaches it to the file "
        "input, optionally submits, and waits for an optional success marker."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("upload", "browser"),
)


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = _get_session(ctx, inputs["session"])

    file_uri = inputs["file_uri"]
    if not file_uri.startswith("aakar://"):
        raise ValueError(
            f"file_uri must be a managed-storage URI starting with 'aakar://', got {file_uri!r}"
        )
    data = ctx.object_store.get(file_uri)

    timeout = int(inputs.get("timeout_ms", 30000))
    file_input_selector = inputs["file_input_selector"]

    fd = tempfile.NamedTemporaryFile(delete=False)
    try:
        fd.write(data)
    finally:
        fd.close()

    try:
        await sess.wait_for(file_input_selector, timeout_ms=timeout)
        await sess.upload(file_input_selector, fd.name)

        submit = inputs.get("submit_selector")
        if submit:
            await sess.click(submit)

        success = inputs.get("success_selector")
        if success:
            await sess.wait_for(success, timeout_ms=timeout)
    finally:
        Path(fd.name).unlink(missing_ok=True)

    return {"file_uri": file_uri}
