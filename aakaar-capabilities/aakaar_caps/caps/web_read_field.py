"""cap.web_read_field — read the value associated with a visible field label.

Handles the CTS Outward instrument grid where the value sits in the row BELOW the
header label (e.g. the account number `50200100550851` is the cell directly under
the `Account No.` header), as well as the common "Label : value" (value beside the
label) and label->input layouts. Resolution is by the label text, so it never
needs the framework's volatile element ids. Read-only.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar_caps.browser.state import get_session
from aakaar_caps.caps._zkutil import JS_HELPERS, safe_evaluate
from aakaar_caps.context import CapabilityContext
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.web_read_field"


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str = Field(description="Browser session handle from an upstream node.")
    label: str = Field(description="Visible field label whose value to read, e.g. 'Account No.'.")
    direction: str = Field(
        default="auto",
        description="Where the value sits relative to the label: 'below' (grid header), "
        "'beside' (next cell), 'input' (associated control), or 'auto' (try below -> beside -> input).",
    )


class _Outputs(BaseModel):
    value: str = Field(description="The text value read for that label ('' if not found).")
    via: str = Field(description="Which strategy matched: below | beside | input | none.")


SPEC = CapabilitySpec(
    ref=CAP_REF,
    description=(
        "Read the value associated with a visible field label (e.g. the account number under the "
        "'Account No.' header in the CTS instrument grid). Resolves by label text — handles "
        "value-below-header, value-beside-label, and label->input layouts. Read-only."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("browser", "read"),
    side_effecting=False,
)


_READ_JS = r"""
(() => {
  __HELPERS__
  const want = norm(__LABEL__);
  const dir = __DIR__;
  const wantS = want.replace(/[\s:.\-_]+$/, "");
  const lm = (t) => { const n = norm(t); return n === want || n.replace(/[\s:.\-_]+$/, "") === wantS; };

  // find the label cell/element (tolerant of a trailing ':' / punctuation)
  const labelEls = Array.from(document.querySelectorAll("span.z-label, label, td, th, div"))
    .filter((el) => visible(el) && lm(el.textContent));
  if (!labelEls.length) return { ok: false, via: "none" };

  function looksLikeValue(s) {
    s = (s || "").trim();
    return s.length > 0 && norm(s) !== want;
  }
  function cellText(td) {
    if (!td) return "";
    const inp = td.querySelector("input,textarea");
    if (inp && (inp.value || "").trim()) return inp.value.trim();
    return (td.textContent || "").trim();
  }

  for (const lab of labelEls) {
    const td = lab.closest("td, th, .z-row-inner");
    // 1) value BELOW: same column index in the next row
    if ((dir === "below" || dir === "auto") && td) {
      const tr = td.closest("tr");
      const cells = tr ? Array.from(tr.children) : [];
      const idx = cells.indexOf(td);
      let nrow = tr ? tr.nextElementSibling : null;
      // skip non-row siblings
      while (nrow && nrow.tagName !== "TR") nrow = nrow.nextElementSibling;
      if (nrow && idx >= 0) {
        const below = nrow.children[idx];
        const v = cellText(below);
        if (looksLikeValue(v)) return { ok: true, via: "below", value: v };
      }
    }
    // 2) value BESIDE: next cell in the same row
    if ((dir === "beside" || dir === "auto") && td) {
      let sib = td.nextElementSibling;
      while (sib && !cellText(sib)) sib = sib.nextElementSibling;
      const v = cellText(sib);
      if (looksLikeValue(v)) return { ok: true, via: "beside", value: v };
    }
    // 3) associated INPUT via for=, child, or sibling
    if (dir === "input" || dir === "auto") {
      let inp = null;
      const forId = lab.getAttribute && lab.getAttribute("for");
      if (forId) inp = document.getElementById(forId);
      if (!inp) inp = lab.querySelector && lab.querySelector("input,textarea");
      if (!inp && lab.parentElement) inp = lab.parentElement.querySelector("input,textarea");
      if (inp && (inp.value || "").trim()) return { ok: true, via: "input", value: inp.value.trim() };
    }
  }
  return { ok: false, via: "none" };
})()
"""


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = get_session(ctx.session_state, inputs["session"])
    label = str(inputs["label"])
    direction = str(inputs.get("direction", "auto"))
    js = (_READ_JS.replace("__HELPERS__", JS_HELPERS)
                  .replace("__LABEL__", json.dumps(label))
                  .replace("__DIR__", json.dumps(direction)))
    res = await safe_evaluate(sess, js)
    if isinstance(res, dict) and res.get("ok"):
        value = str(res.get("value", ""))
        via = str(res.get("via", "none"))
        logger.info("cap.web_read_field ok label=%r via=%s value=%r", label, via, value)
        return {"value": value, "via": via}
    logger.info("cap.web_read_field: no value found for label=%r", label)
    return {"value": "", "via": "none"}
