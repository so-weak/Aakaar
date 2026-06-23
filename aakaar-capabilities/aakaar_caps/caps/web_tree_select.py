"""cap.web_tree_select — navigate a ZK tree menu by a path of node labels.

The CTS Outward left menu is a ZK ``<tree>``: each node is a
``<tr class="z-treerow" aria-label="<node text>">`` whose expand/collapse caret
is a ``<span class="z-tree-icon"><i class="z-icon-caret-right z-tree-close">`` (or
``…caret-down z-tree-open`` when expanded). A child node only exists in the DOM
once its parent is expanded, so reaching a leaf means: expand each ancestor's
caret, wait for the children to render, then click the leaf row.

`browser.click_by_text` clicks the row (selecting it) and cannot toggle the
caret; this capability does both. Give it the ordered labels, e.g.
``path=["E-Callback Processing", "Ecall Back Processing"]`` — every label except
the last is expanded; the last is clicked to open it.
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
CAP_REF = "cap.web_tree_select"

_DEFAULT_TIMEOUT_MS = 20000
_POLL_INTERVAL_S = 0.25


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str = Field(description="Browser session handle from an upstream node.")
    path: list[str] = Field(
        min_length=1,
        description=(
            "Ordered ZK tree node labels from the top of the branch to the target "
            "leaf, e.g. ['E-Callback Processing', 'Ecall Back Processing']. Every "
            "label except the last is expanded (caret clicked); the last is clicked "
            "to open it."
        ),
    )
    timeout_ms: int = Field(
        default=_DEFAULT_TIMEOUT_MS, ge=1000, le=120000,
        description="Per-node wait for the row (and, for parents, its expansion).",
    )


class _Outputs(BaseModel):
    selected: str = Field(description="The leaf node label that was clicked.")
    expanded: list[str] = Field(description="The ancestor labels that were expanded.")


SPEC = CapabilitySpec(
    ref=CAP_REF,
    description=(
        "Navigate a ZK tree menu by a path of node labels: expand each ancestor "
        "node's caret (waiting for children to render) and click the final leaf to "
        "open it. Use for the CTS Outward left menu, e.g. open 'Ecall Back "
        "Processing' under 'E-Callback Processing'. Handles the expand caret that "
        "browser.click_by_text cannot reach."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("browser", "navigate", "tree"),
    side_effecting=True,
)


# Locate a treerow by aria-label (ZK appends " selected" to the active row's
# label, so compare after stripping it). Reports whether it's collapsed and gives
# selectors for its caret icon and the row itself.
_FIND_ROW_JS = r"""
(() => {
  __HELPERS__
  const want = norm(__LABEL__);
  const rows = Array.from(document.querySelectorAll("tr.z-treerow, .z-treerow")).filter(visible);
  const match = rows.find((r) => {
    let al = norm(r.getAttribute("aria-label"));
    if (al.endsWith(" selected")) al = al.slice(0, -" selected".length);
    if (al === want) return true;
    // Fall back to the row's visible label text.
    return norm((r.querySelector(".z-treecell-content .z-label, .z-label") || {}).textContent) === want;
  });
  if (!match) return { ok: false };
  const icon = match.querySelector(".z-tree-icon");
  const expandedAttr = match.getAttribute("aria-expanded");
  const i = match.querySelector(".z-tree-icon i, .z-tree-icon");
  const cls = (i && i.className) || "";
  const collapsed = expandedAttr === "false" || /z-tree-close|caret-right/.test(cls);
  return {
    ok: true,
    row_sel: bestSelector(match),
    icon_sel: icon ? bestSelector(icon) : null,
    collapsed: collapsed,
    has_icon: !!icon,
  };
})()
"""


def _js(label: str) -> str:
    return _FIND_ROW_JS.replace("__HELPERS__", JS_HELPERS).replace("__LABEL__", json.dumps(label))


async def _find_row(sess: Any, label: str, deadline: float) -> dict[str, Any]:
    js = _js(label)
    while True:
        res = await sess.evaluate(js)
        if isinstance(res, dict) and res.get("ok"):
            return res
        if time.monotonic() >= deadline:
            raise RuntimeError(f"cap.web_tree_select: tree node {label!r} not found")
        await asyncio.sleep(_POLL_INTERVAL_S)


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = get_session(ctx.session_state, inputs["session"])
    path = [str(p) for p in inputs["path"]]
    timeout_ms = int(inputs.get("timeout_ms", _DEFAULT_TIMEOUT_MS))
    deadline = time.monotonic() + (timeout_ms / 1000.0)

    logger.info("cap.web_tree_select start run_id=%s session=%s path=%r", ctx.run_id, inputs["session"], path)

    expanded: list[str] = []
    # Expand every ancestor (all but the last label).
    for label in path[:-1]:
        row = await _find_row(sess, label, deadline)
        if row.get("collapsed") and row.get("icon_sel"):
            await click_or_js(sess, str(row["icon_sel"]))
            expanded.append(label)
            await asyncio.sleep(_POLL_INTERVAL_S)  # let ZK render the children

    # Click the leaf row to open it.
    leaf = path[-1]
    row = await _find_row(sess, leaf, deadline)
    await click_or_js(sess, str(row["row_sel"]))
    logger.info("cap.web_tree_select ok path=%r expanded=%r", path, expanded)
    return {"selected": leaf, "expanded": expanded}
