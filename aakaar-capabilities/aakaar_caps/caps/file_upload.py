"""cap.file_upload — attach a managed-storage file to a file input.

Materializes a file from the object store (via the portable read_object seam),
hands it to the browser session, optionally submits, and waits for an optional
success marker. Shared: identical on the server and a remote agent.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar_caps.browser.state import get_session
from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.file_upload"

_STORED_NAME_RE = re.compile(r"^[0-9a-fA-F]{32}_(.+)$")


def _user_facing_basename(file_uri: str) -> str:
    base = file_uri.rsplit("/", 1)[-1] if "/" in file_uri else file_uri
    if not base:
        return "upload.bin"
    m = _STORED_NAME_RE.match(base)
    return m.group(1) if m else base


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str = Field(description="Authenticated browser session handle, e.g. ${login.session}.")
    file_uri: str = Field(
        description=(
            "Managed-storage URI of the file to upload (`aakaar://...`). "
            "Use upstream `${node.uri}` references; do not embed literal paths."
        ),
    )
    file_input_selector: str = Field(description="CSS selector for the <input type='file'> element.")
    submit_selector: str | None = Field(
        default=None,
        description=(
            "Optional CSS selector for a submit/upload button to click after "
            "the file is attached. Omit if the form auto-submits on selection."
        ),
    )
    submit_label: str | None = Field(
        default=None,
        description=(
            "Optional visible text of a submit button (e.g. 'Upload', 'Submit'). "
            "Resolved via click_by_text — use this when you don't have a "
            "verified CSS selector for the submit control. Tried after "
            "`submit_selector` if both are given."
        ),
    )
    success_selector: str | None = Field(
        default=None,
        description=(
            "Optional CSS selector that proves the upload was accepted "
            "(e.g. a success banner). The handler waits for it after submit."
        ),
    )
    success_text: str | None = Field(
        default=None,
        description=(
            "Optional visible text that proves success (e.g. 'Uploaded', "
            "'File received'). Use instead of success_selector when you "
            "don't know the exact CSS class. Polled for up to `timeout_ms`."
        ),
    )
    timeout_ms: int = Field(default=30000, ge=1000, le=300000, description="Selector wait timeout.")


class _Outputs(BaseModel):
    file_uri: str = Field(description="The same file URI that was uploaded (echo).")


SPEC = CapabilitySpec(
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

_SUBMIT_JS = r"""
(needle) => {
  const txt = String(needle).trim().toLowerCase();
  if (!txt) return null;
  const all = Array.from(document.querySelectorAll(
    "form button[type='submit'], form input[type='submit'], "
    + "button[type='submit'], input[type='submit'], button"
  ));
  for (const el of all) {
    if (el.getAttribute('role') === 'tab') continue;
    const t = (el.tagName === 'INPUT' ? (el.value || '') : (el.innerText || el.textContent || '')).toLowerCase();
    const isSubmit = el.matches("button[type='submit'], input[type='submit']");
    if (isSubmit && t.includes(txt)) { el.click(); return true; }
  }
  for (const el of all) {
    if (el.getAttribute('role') === 'tab') continue;
    const t = ((el.innerText || el.textContent || '').toLowerCase());
    if (t.includes(txt)) { el.click(); return true; }
  }
  return false;
}
"""


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = get_session(ctx.session_state, inputs["session"])

    file_uri = inputs["file_uri"]
    if not file_uri.startswith("aakaar://"):
        raise ValueError(
            f"file_uri must be a managed-storage URI starting with 'aakaar://', got {file_uri!r}"
        )
    logger.info("cap.file_upload start run_id=%s session=%s uri=%s", ctx.run_id, inputs["session"], file_uri)
    data = await ctx.read_object(file_uri)

    timeout = int(inputs.get("timeout_ms", 30000))
    file_input_selector = inputs["file_input_selector"]

    display_name = _user_facing_basename(file_uri)
    staging_dir = Path(tempfile.mkdtemp(prefix="aakaar-upload-"))
    staged_path = staging_dir / display_name
    staged_path.write_bytes(data)

    try:
        await sess.wait_for(file_input_selector, timeout_ms=timeout)
        await sess.upload(file_input_selector, str(staged_path))

        submit_selector = inputs.get("submit_selector")
        submit_label = inputs.get("submit_label")
        if submit_selector:
            await sess.click(submit_selector)
        elif submit_label:
            ok = await sess.evaluate(f"({_SUBMIT_JS})({submit_label!r})")
            if not ok:
                raise RuntimeError(f"cap.file_upload: could not find a submit control matching {submit_label!r}")

        success_selector = inputs.get("success_selector")
        success_text = inputs.get("success_text")
        if success_selector:
            await sess.wait_for(success_selector, timeout_ms=timeout)
        elif success_text:
            import asyncio as _asyncio

            deadline = timeout / 1000.0
            interval = 0.5
            elapsed = 0.0
            needle = success_text.lower()
            while elapsed < deadline:
                try:
                    body = await sess.evaluate("(document.body && document.body.innerText) || ''")
                    if isinstance(body, str) and needle in body.lower():
                        break
                except Exception:  # noqa: BLE001
                    pass
                await _asyncio.sleep(interval)
                elapsed += interval
            else:
                raise RuntimeError(
                    f"cap.file_upload: success_text {success_text!r} did not appear within {timeout}ms"
                )
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    logger.info("cap.file_upload ok run_id=%s uri=%s display_name=%s", ctx.run_id, file_uri, display_name)
    return {"file_uri": file_uri}
