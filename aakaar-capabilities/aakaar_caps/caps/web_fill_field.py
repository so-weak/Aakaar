"""cap.web_fill_field — type a value into an input found by its visible label.

Symmetric to cap.web_read_field, but writes. Locates the input associated with a
label — beside the label (e.g. the "Reject Remark :" textbox to the right of the
label), below a header, or via for=/child — and fills it. Resolution is by label
text, so no volatile element ids. Side-effecting (it types).
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
CAP_REF = "cap.web_fill_field"


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str = Field(description="Browser session handle from an upstream node.")
    label: str = Field(description="Visible label of the field to fill, e.g. 'Reject Remark'.")
    value: str = Field(description="Text to type into the resolved input.")
    direction: str = Field(default="auto",
        description="Where the input sits vs the label: 'beside', 'below', 'input', or 'auto'.")


class _Outputs(BaseModel):
    filled: bool = Field(description="True if an input was found and filled.")
    selector: str = Field(description="CSS selector of the filled input ('' if none).")


SPEC = CapabilitySpec(
    ref=CAP_REF,
    description=(
        "Type a value into the input associated with a visible field label (beside / below / for=). "
        "Use for labelled text fields such as the 'Reject Remark' box. Resolves by label text."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("browser", "form", "fill"),
    side_effecting=True,
)


_RESOLVE_JS = r"""
(() => {
  __HELPERS__
  const want = norm(__LABEL__);
  const dir = __DIR__;
  const wantS = want.replace(/[\s:.\-_]+$/, "");
  const lm = (t) => { const n = norm(t); return n === want || n.replace(/[\s:.\-_]+$/, "") === wantS; };
  const labelEls = Array.from(document.querySelectorAll("span.z-label, label, td, th, div"))
    .filter((el) => visible(el) && lm(el.textContent));
  function inputIn(cell) {
    if (!cell) return null;
    const i = cell.querySelector("input:not([type='hidden']):not([readonly]), textarea:not([readonly])");
    return (i && visible(i)) ? i : null;
  }
  for (const lab of labelEls) {
    const td = lab.closest("td, th, .z-row-inner");
    if ((dir === "beside" || dir === "auto") && td) {
      let sib = td.nextElementSibling;
      while (sib && !inputIn(sib)) sib = sib.nextElementSibling;
      const i = inputIn(sib);
      if (i) return { ok: true, selector: bestSelector(i) };
    }
    if ((dir === "below" || dir === "auto") && td) {
      const tr = td.closest("tr"); const cells = tr ? Array.from(tr.children) : [];
      const idx = cells.indexOf(td);
      let nrow = tr ? tr.nextElementSibling : null;
      while (nrow && nrow.tagName !== "TR") nrow = nrow.nextElementSibling;
      const i = (nrow && idx >= 0) ? inputIn(nrow.children[idx]) : null;
      if (i) return { ok: true, selector: bestSelector(i) };
    }
    if (dir === "input" || dir === "auto") {
      let i = null;
      const forId = lab.getAttribute && lab.getAttribute("for");
      if (forId) i = document.getElementById(forId);
      if (!i && lab.parentElement) i = inputIn(lab.parentElement);
      if (i && visible(i)) return { ok: true, selector: bestSelector(i) };
    }
  }
  return { ok: false };
})()
"""


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = get_session(ctx.session_state, inputs["session"])
    label = str(inputs["label"])
    value = str(inputs["value"])
    direction = str(inputs.get("direction", "auto"))
    js = (_RESOLVE_JS.replace("__HELPERS__", JS_HELPERS)
                     .replace("__LABEL__", json.dumps(label))
                     .replace("__DIR__", json.dumps(direction)))
    res = await safe_evaluate(sess, js)
    if isinstance(res, dict) and res.get("ok"):
        selector = str(res["selector"])
        await sess.fill(selector, value)
        logger.info("cap.web_fill_field ok label=%r selector=%s", label, selector)
        return {"filled": True, "selector": selector}
    logger.info("cap.web_fill_field: no input found for label=%r", label)
    return {"filled": False, "selector": ""}
