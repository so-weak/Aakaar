"""cap.file_download — download a file through an authenticated browser session.

Three ways to specify what to download (exactly one): `trigger_selector`, `url`,
or a natural-language `target_hint` (fuzzy-matched against the page, with an HITL
picker when ambiguous). Shared: identical on the server and a remote agent.
Bytes go to the canonical object store via write_object; the optional sibling
mirror only happens server-side (ctx.download_mirror_dir is None on the agent).
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aakaar_caps.browser.session import BrowserSession, DownloadedFile
from aakaar_caps.browser.state import get_session
from aakaar_caps.caps.file_download.discovery import DISCOVERY_JS, Candidate, decide, rank_candidates
from aakaar_caps.context import CapabilityContext, CapabilityError
from aakaar_caps.spec import CapabilitySpec

logger = logging.getLogger(__name__)
CAP_REF = "cap.file_download"


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str = Field(description="Authenticated browser session handle, e.g. ${login.session}.")
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
    timeout_ms: int = Field(default=15000, ge=1000, le=120000, description="Selector wait timeout.")

    @model_validator(mode="after")
    def _check_one_of(self) -> _Inputs:
        provided = sum(1 for v in (self.trigger_selector, self.url, self.target_hint) if v)
        if provided != 1:
            raise ValueError("exactly one of `trigger_selector`, `url`, or `target_hint` must be provided")
        return self


class _Outputs(BaseModel):
    uri: str = Field(description="Managed-storage URI of the downloaded file.")
    filename: str = Field(description="Original filename reported by the browser/server.")


SPEC = CapabilitySpec(
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


async def run(ctx: CapabilityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    sess = get_session(ctx.session_state, inputs["session"])

    wait_selector = inputs.get("wait_for")
    if wait_selector:
        await sess.wait_for(wait_selector, timeout_ms=int(inputs.get("timeout_ms", 15000)))

    target_hint = inputs.get("target_hint")
    trigger_selector = inputs.get("trigger_selector")
    url = inputs.get("url")

    logger.info(
        "cap.file_download start run_id=%s session=%s mode=%s",
        ctx.run_id, inputs["session"],
        "selector" if trigger_selector else ("url" if url else "target_hint"),
    )

    if not target_hint:
        file = await sess.download(trigger_selector=trigger_selector, url=url)
    else:
        file = await _download_with_nav_recovery(ctx, sess, target_hint=target_hint, max_steps=3)

    key = f"runs/{ctx.run_id}/downloads/{uuid.uuid4().hex}_{file.filename}"
    uri = await ctx.write_object(key, file.content)
    mirror_path = _mirror_to_disk(ctx.download_mirror_dir, file.filename, file.content)
    logger.info(
        "cap.file_download ok uri=%s filename=%s bytes=%d mirror=%s",
        uri, file.filename, len(file.content), mirror_path or "-",
    )
    return {"uri": uri, "filename": file.filename}


def _mirror_to_disk(mirror_dir: Any, filename: str, content: bytes) -> Path | None:
    """Write `content` to `mirror_dir` (a server-host dev convenience). None on
    the agent (mirror_dir is None there) or on any failure — the object store
    already holds the canonical copy, so a broken mirror must not fail the run."""
    if mirror_dir is None:
        return None
    try:
        mirror_dir = Path(mirror_dir)
        base = Path(filename).name or "download.bin"
        mirror_dir.mkdir(parents=True, exist_ok=True)
        target = mirror_dir / base
        if target.exists():
            stem, suffix, n = target.stem, target.suffix, 1
            while True:
                candidate = mirror_dir / f"{stem} ({n}){suffix}"
                if not candidate.exists():
                    target = candidate
                    break
                n += 1
        target.write_bytes(content)
        return target
    except Exception:  # noqa: BLE001
        logger.warning("cap.file_download: mirror to %s failed for %r", mirror_dir, filename, exc_info=True)
        return None


async def _download_with_nav_recovery(
    ctx: CapabilityContext, sess: BrowserSession, *, target_hint: str, max_steps: int
) -> DownloadedFile:
    visited: set[str] = set()
    last_error: Exception | None = None
    for step in range(max_steps):
        selector = await _resolve_target_hint(ctx, sess, target_hint, exclude_selectors=visited)
        visited.add(selector)
        try:
            return await sess.download(trigger_selector=selector)
        except Exception as e:  # noqa: BLE001
            last_error = e
            msg = str(e).lower()
            looks_like_nav = "download" in msg or "timeout" in msg or "navigation" in msg
            logger.info(
                "cap.file_download step=%d click=%r looked_like_nav=%s err=%s",
                step, selector, looks_like_nav, type(e).__name__,
            )
            if step == max_steps - 1 or not looks_like_nav:
                raise
            continue
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"cap.file_download: ran out of candidates for {target_hint!r}")  # pragma: no cover


async def _resolve_target_hint(
    ctx: CapabilityContext, sess: Any, target_hint: str, *, exclude_selectors: set[str] | None = None
) -> str:
    raw = await sess.evaluate(DISCOVERY_JS)
    if not isinstance(raw, dict):
        raise RuntimeError(f"file_download discovery returned non-object: {type(raw).__name__}")
    candidates = rank_candidates(raw.get("candidates"), target_hint=target_hint)
    if exclude_selectors:
        candidates = [c for c in candidates if c.selector not in exclude_selectors]
    pick = decide(candidates)

    if pick.chosen is not None:
        return pick.chosen.selector

    if pick.none_match:
        top_5 = candidates[:5]
        sample = "; ".join(
            f"{c.score:.2f} {(c.text or c.aria_label or '?')[:60]!r}" for c in top_5
        ) or "(no interactive elements found)"
        raise RuntimeError(
            f"cap.file_download: no element on the page matches target_hint {target_hint!r} "
            f"(top score below threshold). Top candidates: {sample}. "
            f"Either rephrase the hint, or supply trigger_selector / url."
        )

    chosen = await _ask_human_to_pick(ctx, session=sess, target_hint=target_hint, contenders=pick.ambiguous)
    return chosen.selector


async def _ask_human_to_pick(
    ctx: CapabilityContext, *, session: Any, target_hint: str, contenders: list[Candidate]
) -> Candidate:
    """Screenshot the page, upload it, and ask a human to pick a candidate by
    1-based index via the HITL channel."""
    if ctx.signal_opener is None:
        raise CapabilityError(
            "cap.file_download could not auto-resolve target_hint and no HITL channel is wired"
        )
    image_bytes = await session.screenshot()
    key = f"runs/{ctx.run_id}/picker/{ctx.node_id}_{uuid.uuid4().hex}.png"
    uri = await ctx.write_object(key, image_bytes)

    lines = [f"Multiple candidates match {target_hint!r}. Reply with a number (1-{len(contenders)}):"]
    for i, c in enumerate(contenders, start=1):
        label = c.text or c.aria_label or "(no text)"
        lines.append(f"  {i}. {label[:120]}")
    lines.append(f"Page screenshot: {uri}")

    raw = await ctx.open_signal("\n".join(lines), "text")
    pick_idx = _parse_index(raw, n=len(contenders))
    if pick_idx is None:
        raise RuntimeError(f"cap.file_download: invalid pick {raw!r}; expected a number 1-{len(contenders)}")
    return contenders[pick_idx - 1]


def _parse_index(raw: str, *, n: int) -> int | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        i = int(s)
    except ValueError:
        cleaned = "".join(ch for ch in s if ch.isdigit())
        if not cleaned:
            return None
        i = int(cleaned)
    return i if 1 <= i <= n else None
