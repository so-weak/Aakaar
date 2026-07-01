"""cap.cts_back_image_harvest — download every cheque BACK image in a fetched
batch, named by its recorded account number, and return a single ZIP.

For each instrument shown after Fetch: read the recorded account number, open the
cheque BACK image, download it (the zkau image bytes, with a screenshot fallback),
and stash it as ``<account>.<ext>``. Advance with "Next Instrument" and repeat
until the "No record found" popup appears OR an image URL repeats (the portal
wrapped around). Bundles everything into one ZIP (plus a manifest.csv) in managed
storage. Does NOT accept/reject — it only views and downloads. Runs on the agent.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import zipfile
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar_caps.browser.state import get_session
from aakaar_caps.caps import web_click, web_read_field
from aakaar_caps.caps._zkutil import JS_HELPERS, safe_evaluate
from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.cts_back_image_harvest"


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str = Field(description="Browser session handle (post-login, post-Fetch, instrument shown).")
    delay_seconds: float = Field(default=5.0, ge=0, le=60, description="Delay between UI steps.")
    zip_filename: str = Field(default="back_images.zip", description="Name of the output ZIP.")
    write_zip: bool = Field(default=True,
        description="Write the ZIP here. Set False when an outer sweep consolidates images itself.")
    truth_label: str = Field(default="Account No.", description="Label of the recorded account-number column.")
    back_image: str = Field(default="image_back", description="Image hint for the Back-image button.")
    next_image: str = Field(default="image_skip", description="Image hint for the Next-Instrument button.")
    cheque_selector: str = Field(default="img.z-image[src*='zkau/view']", description="CSS selector of the cheque <img>.")
    no_record_text: str = Field(default="No record found", description="Popup text that ends the harvest.")
    ok_label: str = Field(default="OK", description="OK button text on the terminal popup.")


class _Outputs(BaseModel):
    zip_uri: str = Field(description="Managed-storage URI of the ZIP of back images ('' if none).")
    count: int = Field(description="Number of back images downloaded.")
    accounts: list[str] = Field(default_factory=list, description="Account numbers harvested (filenames sans ext).")
    stopped_reason: str = Field(description="'no_record_found' | 'wrapped' (image repeated).")


SPEC = CapabilitySpec(
    ref=CAP_REF,
    description=(
        "Download every cheque BACK image in a fetched CTS batch, named by its recorded account "
        "number, advancing with Next Instrument until 'No record found' (or until an image repeats), "
        "and return a single ZIP (with manifest.csv) in managed storage. View/download only — no "
        "accept/reject. Runs on the agent."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("browser", "cheque", "download", "zip"),
    side_effecting=True,
)

_POPUP_JS = r"""
(() => {
  __HELPERS__
  const want = norm(__TEXT__);
  return Array.from(document.querySelectorAll("*")).some(
    (e) => visible(e) && (e.textContent || "").length < 120 && norm(e.textContent).includes(want));
})()
"""

_SRC_JS = r"""
(() => {
  let e = null;
  try { e = document.querySelector(__SEL__); } catch (x) { e = null; }
  return e ? (e.currentSrc || e.getAttribute("src") || "") : "";
})()
"""


async def _popup(sess: Any, text: str) -> bool:
    js = _POPUP_JS.replace("__HELPERS__", JS_HELPERS).replace("__TEXT__", json.dumps(text))
    try:
        return bool(await safe_evaluate(sess, js))
    except Exception:  # noqa: BLE001
        return False


async def _src(sess: Any, selector: str) -> str:
    js = _SRC_JS.replace("__SEL__", json.dumps(selector))
    try:
        return str(await safe_evaluate(sess, js) or "")
    except Exception:  # noqa: BLE001
        return ""


def _ext_from_src(src: str) -> str:
    low = src.lower()
    if ".jpeg" in low or ".jpg" in low:
        return "jpg"
    if ".gif" in low:
        return "gif"
    return "png"


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "unknown"


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = get_session(ctx.session_state, inputs["session"])
    sid = str(inputs["session"])
    delay = float(inputs.get("delay_seconds", 5.0))
    zip_filename = str(inputs.get("zip_filename", "back_images.zip"))
    write_zip = bool(inputs.get("write_zip", True))
    truth_label = str(inputs.get("truth_label", "Account No."))
    back_image = str(inputs.get("back_image", "image_back"))
    next_image = str(inputs.get("next_image", "image_skip"))
    cheque_selector = str(inputs.get("cheque_selector", "img.z-image[src*='zkau/view']"))
    no_record_text = str(inputs.get("no_record_text", "No record found"))
    ok_label = str(inputs.get("ok_label", "OK"))

    async def settle() -> None:
        if delay > 0:
            await asyncio.sleep(delay)

    images: list[tuple[str, bytes]] = []   # (filename, bytes)
    manifest: list[tuple[str, str, str]] = []  # (filename, account, src)
    used_names: set[str] = set()
    seen_srcs: set[str] = set()
    accounts: list[str] = []
    stopped = "no_record_found"
    logger.info("cap.cts_back_image_harvest start run_id=%s", ctx.run_id)

    while True:
        if await _popup(sess, no_record_text):
            stopped = "no_record_found"
            break

        truth = (await web_read_field.run(
            ctx, {"session": sid, "label": truth_label, "direction": "below"})).get("value", "")

        await web_click.run(ctx, {"session": sid, "image": back_image})  # show the BACK image
        await settle()

        src = await _src(sess, cheque_selector)
        if src and src in seen_srcs:           # portal wrapped back to a cheque we already grabbed
            stopped = "wrapped"
            break
        if src:
            seen_srcs.add(src)

        # Download the back-image bytes; fall back to a rasterized screenshot.
        data: bytes | None = None
        if src:
            try:
                data = (await sess.download(url=src)).content
            except Exception:  # noqa: BLE001
                data = None
        ext = _ext_from_src(src) if data else "png"
        if not data:
            data = await sess.screenshot_element(cheque_selector)

        acct = (truth or "").strip() or f"unknown_{len(images) + 1}"
        base = _safe(acct)
        fname = f"{base}.{ext}"
        n = 2
        while fname in used_names:
            fname = f"{base}_{n}.{ext}"
            n += 1
        used_names.add(fname)
        images.append((fname, data))
        manifest.append((fname, acct, src))
        accounts.append(acct)
        logger.info("harvest: account=%s -> %s (%d bytes)", acct, fname, len(data))

        await web_click.run(ctx, {"session": sid, "image": next_image})  # Next Instrument
        await settle()

    # Dismiss the terminal "No record found" popup with OK so the criteria form
    # reappears for the next batch (an outer sweep refills it). Harmless no-op if
    # the loop ended without a popup up (e.g. a wrap).
    if await _popup(sess, no_record_text):
        try:
            await web_click.run(ctx, {"session": sid, "text": ok_label})
            await settle()
        except Exception:  # noqa: BLE001
            pass

    zip_uri = ""
    if write_zip and images:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname, data in images:
                zf.writestr(fname, data)
            man = "filename,account,image_url\n" + "\n".join(
                f"{f},{a},{u}" for f, a, u in manifest)
            zf.writestr("manifest.csv", man)
        zip_uri = await ctx.write_object(f"runs/{ctx.run_id}/back_images/{zip_filename}", buf.getvalue())

    logger.info("cap.cts_back_image_harvest done count=%d stop=%s uri=%s", len(images), stopped, zip_uri)
    result: dict[str, Any] = {"zip_uri": zip_uri, "count": len(images),
                              "accounts": accounts, "stopped_reason": stopped}
    if not write_zip:
        # Hand raw images to an outer sweep (in-process call) for one consolidated ZIP.
        result["images_raw"] = [(images[i][0], images[i][1], manifest[i][1], manifest[i][2])
                                for i in range(len(images))]
    return result
