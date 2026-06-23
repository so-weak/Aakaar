"""cap.web_select — choose a value in a dropdown identified by its field label.

Handles the two dropdown shapes the CTS Outward forms use:

  * **ZK combobox** — `<span class="z-combobox"><input class="z-combobox-input"
    readonly><a class="z-combobox-button">…</a><div class="z-combobox-popup"><ul>
    <li class="z-comboitem"><span class="z-comboitem-text">VALUE</span></li>…`.
    The input is **readonly** (you cannot type into it), and the value only
    registers when the matching popup `<li>` is clicked — which is why
    `browser.set_field` / `cap.web_form_fill` (both of which `fill()` the input)
    cannot drive it. This capability opens the popup and clicks the item whose
    text matches `value`.
  * **native `<select>`** — falls back to normal option selection.

The control is found by its **visible field label** (e.g. "Record Type"), so the
DAG never needs the framework's volatile element ids. Operates on an existing
session; chain after the page/form is open.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar_caps.browser.state import get_session
from aakaar_caps.caps._zkutil import JS_HELPERS, click_or_js
from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.web_select"

_DEFAULT_TIMEOUT_MS = 15000
_POLL_INTERVAL_S = 0.25


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str = Field(description="Browser session handle from an upstream node.")
    label: str = Field(
        description=(
            "Visible field label of the dropdown to set, exactly as shown on the "
            "page (e.g. 'Record Type', 'Core System'). The control is located "
            "relative to this label, so no CSS selector is needed."
        )
    )
    value: str = Field(
        description="The option text to choose (e.g. 'TXN', '19-JUN-2026', '06')."
    )
    timeout_ms: int = Field(
        default=_DEFAULT_TIMEOUT_MS, ge=1000, le=120000,
        description="How long to wait for the control and the option to appear.",
    )


class _Outputs(BaseModel):
    selected: str = Field(description="The value that was chosen.")
    label: str = Field(description="The field label the dropdown was found by.")
    kind: str = Field(description="'zk_combobox' or 'select'.")


SPEC = CapabilitySpec(
    ref=CAP_REF,
    description=(
        "Choose a value in a dropdown found by its visible field label. Supports "
        "ZK comboboxes (readonly input + popup list — opens the popup and clicks "
        "the matching item) and native <select> elements. Use this for ZK form "
        "fields such as Processing Date, Record Type, Core System, Cycle No — "
        "browser.set_field/cap.web_form_fill cannot set a readonly ZK combobox."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("browser", "form", "select"),
    side_effecting=True,
)


# Resolve the dropdown control sitting next to a field label. Returns the kind
# plus document-unique selectors for the pieces the handler needs to drive.
_RESOLVE_JS = r"""
(() => {
  __HELPERS__
  const want = norm(__LABEL__);

  // 1. Find the label element (prefer ZK labels / <label> / table cells).
  const labelEls = Array.from(
    document.querySelectorAll("span.z-label, label, td, th, div")
  ).filter((el) => visible(el) && norm(el.textContent) === want);
  if (!labelEls.length) return { ok: false, reason: "label_not_found" };

  // 2. From each candidate label, look for a dropdown in the same row/container.
  const SCOPE = "tr, .z-row, .z-cell, .z-row-content, .field, .form-group, .z-hbox";
  for (const lab of labelEls) {
    let scope = lab.closest(SCOPE) || lab.parentElement;
    for (let up = 0; scope && up < 4; up++) {
      const combo = scope.querySelector(".z-combobox");
      if (combo && visible(combo)) {
        const btn = combo.querySelector(".z-combobox-button") ||
                    combo.querySelector("a[role='button']") ||
                    combo.querySelector("input.z-combobox-input");
        const popup = combo.querySelector(".z-combobox-popup");
        return {
          ok: true, kind: "zk_combobox",
          button_sel: bestSelector(btn || combo),
          popup_sel: popup ? bestSelector(popup) : null,
        };
      }
      const sel = scope.querySelector("select");
      if (sel && visible(sel)) {
        return { ok: true, kind: "select", select_sel: bestSelector(sel) };
      }
      scope = scope.parentElement;
    }
  }
  return { ok: false, reason: "no_dropdown_near_label" };
})()
"""

# Find the popup item matching `value` (popup must be open/visible). The popup
# selector is optional and may be empty: ZK creates the popup lazily (it does not
# exist when the combobox is first resolved) and often renders it at document.body
# rather than inside the combobox. So when no usable popup selector is given, scan
# the whole document for *visible* combo items — only the open popup's items are
# visible, so this stays unambiguous. (document.querySelector("") throws, hence the
# empty-string guard.)
_FIND_ITEM_JS = r"""
(() => {
  __HELPERS__
  const want = norm(__VALUE__);
  const sel = __POPUP__;
  let root = document;
  if (sel) {
    try { const p = document.querySelector(sel); if (p && visible(p)) root = p; } catch (e) {}
  }
  const items = Array.from(root.querySelectorAll("li.z-comboitem, .z-comboitem")).filter(visible);
  if (!items.length) return { ok: false };
  const text = (el) => norm((el.querySelector(".z-comboitem-text") || el).textContent);
  const hit = items.find((el) => text(el) === want) ||
              items.find((el) => text(el).indexOf(want) >= 0);
  if (!hit) return { ok: false };
  try { hit.scrollIntoView({ block: "center" }); } catch (e) {}
  return { ok: true, item_sel: bestSelector(hit) };
})()
"""


def _js(template: str, **subs: str) -> str:
    out = template.replace("__HELPERS__", JS_HELPERS)
    for k, v in subs.items():
        out = out.replace(f"__{k}__", json.dumps(v))
    return out


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = get_session(ctx.session_state, inputs["session"])
    label = str(inputs["label"])
    value = str(inputs["value"])
    timeout_ms = int(inputs.get("timeout_ms", _DEFAULT_TIMEOUT_MS))
    deadline = time.monotonic() + (timeout_ms / 1000.0)

    logger.info(
        "cap.web_select start run_id=%s session=%s label=%r value=%r",
        ctx.run_id, inputs["session"], label, value,
    )

    # 1. Locate the control (poll — the form may still be rendering).
    resolve_js = _js(_RESOLVE_JS, LABEL=label)
    info: dict[str, Any] | None = None
    while True:
        res = await sess.evaluate(resolve_js)
        if isinstance(res, dict) and res.get("ok"):
            info = res
            break
        if time.monotonic() >= deadline:
            reason = res.get("reason") if isinstance(res, dict) else "unknown"
            raise RuntimeError(f"cap.web_select: no dropdown for label {label!r} ({reason})")
        await asyncio.sleep(_POLL_INTERVAL_S)

    # 2a. Native <select>: select_option handles value/label matching.
    if info["kind"] == "select":
        await sess.select(str(info["select_sel"]), value)
        logger.info("cap.web_select ok (select) label=%r value=%r", label, value)
        return {"selected": value, "label": label, "kind": "select"}

    # 2b. ZK combobox: open the popup, then click the matching item.
    await click_or_js(sess, str(info["button_sel"]))
    popup_sel = info.get("popup_sel") or ""
    find_js = _js(_FIND_ITEM_JS, VALUE=value, POPUP=popup_sel)
    item_sel: str | None = None
    while True:
        res = await sess.evaluate(find_js)
        if isinstance(res, dict) and res.get("ok"):
            item_sel = str(res["item_sel"])
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"cap.web_select: combobox {label!r} has no option matching {value!r}"
            )
        await asyncio.sleep(_POLL_INTERVAL_S)

    await click_or_js(sess, item_sel)
    logger.info("cap.web_select ok (zk_combobox) label=%r value=%r", label, value)
    return {"selected": value, "label": label, "kind": "zk_combobox"}
