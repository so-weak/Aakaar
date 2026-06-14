"""cap.file_download — download a file through an authenticated browser session.

Three ways to specify what to download (exactly one must be supplied):
  - `trigger_selector` — explicit CSS selector for a link or button.
    Use when you already know the page structure.
  - `url` — direct authenticated download URL.
  - `target_hint` — natural-language description of the report
    ("Biller Transactions May 2026", "first report", "today's settlement").
    The handler walks the post-login page, fuzzy-matches the hint against
    visible link/button text + accessible labels + surrounding row
    context, and clicks the best candidate. If two candidates score
    close, it pauses HITL with a numbered list so the user picks.

The capability does NOT log in. It expects a `session` produced by an
upstream node (typically `cap.web_login`).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aakaar.capabilities.file_download.discovery import (
    DISCOVERY_JS,
    Candidate,
    decide,
    rank_candidates,
)
from aakaar.interpreter.activities.browser import _get_session
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import CapabilityDefinition

logger = logging.getLogger(__name__)
CAP_REF = "cap.file_download"

_HITL_PROMPT_TIMEOUT_S = 300


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str = Field(
        description="Authenticated browser session handle, e.g. ${login.session}."
    )
    trigger_selector: str | None = Field(
        default=None,
        description=(
            "CSS selector for a link/button that initiates the download when "
            "clicked. Mutually exclusive with `url` and `target_hint`."
        ),
    )
    url: str | None = Field(
        default=None,
        description=(
            "Direct download URL the authenticated session can fetch. "
            "Mutually exclusive with `trigger_selector` and `target_hint`."
        ),
    )
    target_hint: str | None = Field(
        default=None,
        description=(
            "Natural-language description of the report to download "
            "(e.g. 'Biller Transactions — May 2026'). The handler walks "
            "the page, fuzzy-matches against visible text, and clicks the "
            "best candidate. Pauses HITL when ambiguous. Mutually exclusive "
            "with `trigger_selector` and `url`."
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
    def _check_one_of(self) -> _Inputs:
        provided = sum(
            1 for v in (self.trigger_selector, self.url, self.target_hint) if v
        )
        if provided != 1:
            raise ValueError(
                "exactly one of `trigger_selector`, `url`, or `target_hint` must be provided"
            )
        return self


class _Outputs(BaseModel):
    uri: str = Field(description="Managed-storage URI of the downloaded file.")
    filename: str = Field(description="Original filename reported by the browser/server.")


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Download a file through an authenticated browser session and store "
        "it in managed storage. Selectors are auto-discovered when "
        "`target_hint` is given (the natural-language name of the report). "
        "Returns the storage URI."
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

    target_hint = inputs.get("target_hint")
    trigger_selector = inputs.get("trigger_selector")
    url = inputs.get("url")

    logger.info(
        "cap.file_download start run_id=%s session=%s mode=%s",
        ctx.run_id,
        inputs["session"],
        "selector" if trigger_selector else ("url" if url else "target_hint"),
    )

    # If a literal selector or URL was supplied, take the simple path —
    # no discovery loop, no nav-then-rescan recovery. The caller knows
    # what they want.
    if not target_hint:
        file = await sess.download(trigger_selector=trigger_selector, url=url)
    else:
        # target_hint path — try discovery, click, see if a download
        # fires. Many sites bury reports behind a section page (e.g.
        # nbbl's "Master Data" → "Biller Master"); the first picker hit
        # may navigate instead of triggering a download. Catch that,
        # re-scan the new page, and try the next match. Capped to a
        # small number of iterations so a misconfigured page can't loop
        # forever.
        logger.debug("cap.file_download target_hint=%r", target_hint)
        file = await _download_with_nav_recovery(
            ctx, sess, target_hint=target_hint, max_steps=3
        )

    key = f"runs/{ctx.run_id}/downloads/{uuid.uuid4().hex}_{file.filename}"
    obj = ctx.object_store.put(str(ctx.tenant_id), key, file.content)
    mirror_path = _mirror_to_disk(ctx.download_mirror_dir, file.filename, file.content)
    logger.info(
        "cap.file_download ok uri=%s filename=%s bytes=%d mirror=%s",
        obj.uri,
        file.filename,
        len(file.content),
        mirror_path or "-",
    )
    return {"uri": obj.uri, "filename": file.filename}


def _mirror_to_disk(
    mirror_dir: Path | None, filename: str, content: bytes
) -> Path | None:
    """Write `content` to `mirror_dir` on the worker host. Returns the
    final path, or None if mirroring is disabled or fails.

    Filename is sanitized to its basename so a malicious server can't
    write outside the mirror dir via "../etc/passwd". On collision we
    append "(1)", "(2)", etc. — same convention as a browser. Errors
    are swallowed (logged) because the object store already holds the
    canonical copy; a broken mirror must not fail the run.
    """
    if mirror_dir is None:
        return None
    try:
        base = Path(filename).name or "download.bin"
        mirror_dir.mkdir(parents=True, exist_ok=True)
        target = mirror_dir / base
        if target.exists():
            stem = target.stem
            suffix = target.suffix
            n = 1
            while True:
                candidate = mirror_dir / f"{stem} ({n}){suffix}"
                if not candidate.exists():
                    target = candidate
                    break
                n += 1
        target.write_bytes(content)
        return target
    except Exception:  # noqa: BLE001
        logger.warning(
            "cap.file_download: mirror to %s failed for %r",
            mirror_dir,
            filename,
            exc_info=True,
        )
        return None


async def _download_with_nav_recovery(
    ctx: ActivityContext, sess: Any, *, target_hint: str, max_steps: int
):
    """Pick a candidate matching `target_hint`, click it, and either
    capture the download or — if the click was a navigation — re-scan
    the new page and try again.

    On each iteration we exclude selectors we've already clicked so a
    bad picker decision doesn't trap us in a loop on the same element.
    """
    visited: set[str] = set()
    last_error: Exception | None = None
    for step in range(max_steps):
        selector = await _resolve_target_hint(
            ctx, sess, target_hint, exclude_selectors=visited
        )
        visited.add(selector)
        logger.debug(
            "cap.file_download step=%d selector=%r target_hint=%r",
            step,
            selector,
            target_hint,
        )
        try:
            return await sess.download(trigger_selector=selector)
        except Exception as e:  # noqa: BLE001
            # Playwright raises TimeoutError when `expect_download` fires
            # nothing within its budget — that's our "click was a nav,
            # not a download" signal. Other exceptions (selector not
            # found, network error) we also retry once with a different
            # candidate, since the page state has likely changed.
            last_error = e
            msg = str(e).lower()
            looks_like_nav = (
                "download" in msg
                or "timeout" in msg
                or "navigation" in msg
            )
            logger.info(
                "cap.file_download step=%d click=%r looked_like_nav=%s err=%s",
                step,
                selector,
                looks_like_nav,
                type(e).__name__,
            )
            if step == max_steps - 1 or not looks_like_nav:
                raise
            # Otherwise loop and let _resolve_target_hint re-discover
            # against the now-changed page.
            continue
    # Defensive: the loop always either returns or re-raises, but keep
    # the type checker happy.
    if last_error is not None:
        raise last_error
    raise RuntimeError(  # pragma: no cover
        f"cap.file_download: ran out of candidates for {target_hint!r}"
    )


# ---------- target_hint resolution -------------------------------------------


async def _resolve_target_hint(
    ctx: ActivityContext,
    sess: Any,
    target_hint: str,
    *,
    exclude_selectors: set[str] | None = None,
) -> str:
    """Walk the page, rank candidates, return a CSS selector to click.

    Pauses HITL with a screenshot + numbered candidate list when the top
    candidate isn't a clear winner. Raises if no candidates match.

    `exclude_selectors`, when supplied, drops candidates whose selector
    matches anything in the set. The nav-recovery loop uses this to
    avoid re-clicking a candidate that already turned out to be a nav
    link rather than a download trigger.
    """
    raw = await sess.evaluate(DISCOVERY_JS)
    if not isinstance(raw, dict):
        raise RuntimeError(
            f"file_download discovery returned non-object: {type(raw).__name__}"
        )
    candidates = rank_candidates(raw.get("candidates"), target_hint=target_hint)
    if exclude_selectors:
        candidates = [c for c in candidates if c.selector not in exclude_selectors]
    pick = decide(candidates)

    if pick.chosen is not None:
        return pick.chosen.selector

    if pick.none_match:
        # Surface the highest-scoring candidates so the operator can
        # diagnose without inspecting the live page. Without this, the
        # error is "nothing matched" with zero context — useless.
        top_5 = candidates[:5]
        sample = "; ".join(
            f"{c.score:.2f} {(c.text or c.aria_label or '?')[:60]!r}"
            for c in top_5
        ) or "(no interactive elements found)"
        raise RuntimeError(
            f"cap.file_download: no element on the page matches target_hint "
            f"{target_hint!r} (top score below threshold). "
            f"Top candidates: {sample}. "
            f"Either rephrase the hint, or supply trigger_selector / url."
        )

    # Ambiguous — surface to a human.
    chosen = await _ask_human_to_pick(
        ctx,
        session=sess,
        target_hint=target_hint,
        contenders=pick.ambiguous,
    )
    return chosen.selector


async def _ask_human_to_pick(
    ctx: ActivityContext,
    *,
    session: Any,
    target_hint: str,
    contenders: list[Candidate],
) -> Candidate:
    """Take a screenshot of the page, save it to managed storage, and
    open a SignalHub prompt listing the candidates. The user replies with
    a 1-based index. Returns the chosen candidate.
    """
    if ctx.signals is None or not ctx.node_id:
        raise RuntimeError(
            "cap.file_download could not auto-resolve target_hint and no "
            "SignalHub is wired to ask a human; this run wasn't started "
            "through the executor's HITL path"
        )

    image_bytes = await session.screenshot()
    key = f"runs/{ctx.run_id}/picker/{ctx.node_id}_{uuid.uuid4().hex}.png"
    obj = ctx.object_store.put(str(ctx.tenant_id), key, image_bytes)

    lines = [
        f"Multiple candidates match {target_hint!r}. Reply with a number (1-{len(contenders)}):"
    ]
    for i, c in enumerate(contenders, start=1):
        label = c.text or c.aria_label or "(no text)"
        lines.append(f"  {i}. {label[:120]}")
    lines.append(f"Page screenshot: {obj.uri}")
    message = "\n".join(lines)

    prompt = await ctx.signals.open(
        run_id=ctx.run_id,
        node_id=ctx.node_id,
        message=message,
        expects="text",
    )
    try:
        raw = await asyncio.wait_for(prompt.future, timeout=_HITL_PROMPT_TIMEOUT_S)
    except TimeoutError as e:
        raise RuntimeError(
            f"cap.file_download picker timed out after {_HITL_PROMPT_TIMEOUT_S}s"
        ) from e

    pick_idx = _parse_index(raw, n=len(contenders))
    if pick_idx is None:
        raise RuntimeError(
            f"cap.file_download: invalid pick {raw!r}; expected a number 1-{len(contenders)}"
        )
    return contenders[pick_idx - 1]


def _parse_index(raw: str, *, n: int) -> int | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        i = int(s)
    except ValueError:
        # Accept "#3" / "3." / "third" — the first one is enough; the
        # last is too cute and we'd rather error out than guess wrong.
        cleaned = "".join(ch for ch in s if ch.isdigit())
        if not cleaned:
            return None
        i = int(cleaned)
    if 1 <= i <= n:
        return i
    return None
