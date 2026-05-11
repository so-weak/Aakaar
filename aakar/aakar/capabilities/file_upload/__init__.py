"""cap.file_upload — attach a managed-storage file to a file input.

Materializes a file from the tenant's object store, hands it to the
browser session via `<input type=file>`, optionally clicks a submit
control, and (optionally) waits for a confirmation selector so the run
doesn't continue before the upload has actually been accepted.

The capability does NOT log in. It expects a `session` produced by an
upstream node (typically `cap.web_login`).
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakar.interpreter.activities.browser import _get_session
from aakar.interpreter.activities.types import ActivityContext
from aakar.shared.registry import CapabilityDefinition


logger = logging.getLogger(__name__)
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
    logger.info(
        "cap.file_upload start run_id=%s session=%s uri=%s selector=%r",
        ctx.run_id,
        inputs["session"],
        file_uri,
        inputs["file_input_selector"],
    )
    data = ctx.object_store.get(file_uri)

    timeout = int(inputs.get("timeout_ms", 30000))
    file_input_selector = inputs["file_input_selector"]

    # Preserve the original suffix on the temp file. Servers (and our
    # admin-app's recon endpoint among them) typically validate the
    # uploaded filename's extension; a tempfile with no extension fails
    # the check even when the bytes are perfectly valid CSV/ZIP.
    suffix = Path(file_uri).suffix
    fd = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        fd.write(data)
    finally:
        fd.close()

    try:
        await sess.wait_for(file_input_selector, timeout_ms=timeout)
        await sess.upload(file_input_selector, fd.name)

        submit_selector = inputs.get("submit_selector")
        submit_label = inputs.get("submit_label")
        if submit_selector:
            await sess.click(submit_selector)
        elif submit_label:
            # Label-based fallback. We DELIBERATELY don't use the
            # generic click_by_text here — pages that ship a tab
            # called "Upload" alongside a submit button called
            # "Upload" (admin-app's recon page is one) make plain
            # text-match ambiguous. JS-resolve to a real submit
            # control: form button[type=submit] containing the
            # label text, or input[type=submit] with that value.
            js = r"""
            (needle) => {
              const txt = String(needle).trim().toLowerCase();
              if (!txt) return null;
              // Prefer form-scoped submit buttons; ranked by:
              //   1) button[type=submit] containing text
              //   2) input[type=submit] with matching value
              //   3) any button[type=submit]
              //   4) any <button> containing the text and not a tab
              const all = Array.from(document.querySelectorAll(
                "form button[type='submit'], form input[type='submit'], "
                + "button[type='submit'], input[type='submit'], button"
              ));
              for (const el of all) {
                const role = el.getAttribute('role');
                if (role === 'tab') continue;
                const t = (el.tagName === 'INPUT'
                  ? (el.value || '')
                  : (el.innerText || el.textContent || '')
                ).toLowerCase();
                const isSubmit =
                  el.matches("button[type='submit'], input[type='submit']");
                if (isSubmit && t.includes(txt)) {
                  el.click();
                  return true;
                }
              }
              for (const el of all) {
                const role = el.getAttribute('role');
                if (role === 'tab') continue;
                const t = ((el.innerText || el.textContent || '')
                  .toLowerCase());
                if (t.includes(txt)) {
                  el.click();
                  return true;
                }
              }
              return false;
            }
            """
            ok = await sess.evaluate(f"({js})({submit_label!r})")
            if not ok:
                raise RuntimeError(
                    f"cap.file_upload: could not find a submit control "
                    f"matching {submit_label!r}"
                )

        success_selector = inputs.get("success_selector")
        success_text = inputs.get("success_text")
        if success_selector:
            await sess.wait_for(success_selector, timeout_ms=timeout)
        elif success_text:
            # Poll for the text to appear in the page body. Cheaper than
            # adding a `wait_for_text` primitive — this runs once after
            # submit and exits as soon as the text is visible.
            import asyncio as _asyncio

            deadline = timeout / 1000.0
            interval = 0.5
            elapsed = 0.0
            needle = success_text.lower()
            while elapsed < deadline:
                try:
                    body = await sess.evaluate(
                        "(document.body && document.body.innerText) || ''"
                    )
                    if isinstance(body, str) and needle in body.lower():
                        break
                except Exception:  # noqa: BLE001
                    pass
                await _asyncio.sleep(interval)
                elapsed += interval
            else:
                raise RuntimeError(
                    f"cap.file_upload: success_text {success_text!r} did "
                    f"not appear within {timeout}ms"
                )
    finally:
        Path(fd.name).unlink(missing_ok=True)

    logger.info("cap.file_upload ok run_id=%s uri=%s", ctx.run_id, file_uri)
    return {"file_uri": file_uri}
