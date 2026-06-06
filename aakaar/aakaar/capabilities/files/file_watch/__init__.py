"""cap.file_watch — bounded poll of object storage for create/modify/delete.

DAG-friendly watch: this is a *bounded* poll, NOT an infinite watcher. It
takes a snapshot of the tenant's object listing under `prefix`, then polls
on a fixed cadence until it observes at least one create / modify / delete
relative to that snapshot, or until `timeout_s` elapses — whichever comes
first. It then returns the set of changes it saw.

This keeps the capability composable inside a DAG: a node either fires when
something arrives/changes within the window, or returns an empty change set
on timeout so the planner can branch on "nothing happened". There is no
long-lived background subscription; nothing leaks past the node.

Detection model (per object key under the prefix):
  - create: key present now, absent in the baseline snapshot.
  - delete: key absent now, present in the baseline snapshot.
  - modify: key present in both, but its content fingerprint changed.

The content fingerprint is the object's sha256 when available, falling back
to byte size (the cheap `list()` path leaves sha256 empty, so we `stat()`
each candidate to get a real digest — over a watched prefix that is a small,
bounded set). Comparing digests means a same-size overwrite still counts as
a modify, and an unchanged poll never produces a false positive.

Returns `{changes: [{key, kind}], changed: bool, polls: int,
elapsed_s: float}`. `changes` is sorted by key for deterministic output.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aakaar.interpreter.activities.types import ActivityContext
from aakaar.shared.registry import CapabilityDefinition

logger = logging.getLogger(__name__)
CAP_REF = "cap.file_watch"

_DEFAULT_TIMEOUT_S = 10.0
_DEFAULT_POLL_MS = 500
_MIN_POLL_MS = 50


class _Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prefix: str = Field(
        default="",
        description=(
            "Object-key prefix to watch within the tenant store (e.g. "
            "'inbox/' or 'reports/2026/'). Empty string watches the whole "
            "tenant namespace. Matched as a literal string prefix on keys."
        ),
    )
    timeout_s: float = Field(
        default=_DEFAULT_TIMEOUT_S,
        gt=0,
        le=600,
        description=(
            "Maximum time to wait for a change before returning an empty "
            "change set. The watch is bounded by this — it never blocks "
            "indefinitely."
        ),
    )
    poll_ms: int = Field(
        default=_DEFAULT_POLL_MS,
        ge=_MIN_POLL_MS,
        le=60000,
        description="Delay between successive listing snapshots, in milliseconds.",
    )


class _Change(BaseModel):
    key: str = Field(description="Object key that changed (relative to tenant root).")
    kind: str = Field(description="One of 'create', 'modify', 'delete'.")


class _Outputs(BaseModel):
    changes: list[_Change] = Field(
        description="Detected changes, sorted by key. Empty when the watch timed out."
    )
    changed: bool = Field(description="True if at least one change was detected.")
    polls: int = Field(description="Number of poll iterations performed after the baseline.")
    elapsed_s: float = Field(description="Wall-clock seconds spent watching.")


definition = CapabilityDefinition(
    ref=CAP_REF,
    description=(
        "Bounded watch of tenant object storage under a key prefix. Snapshots "
        "the current listing, then polls until a create / modify / delete is "
        "observed or a timeout elapses, and returns the change set. Not an "
        "infinite watcher — it always returns within timeout_s, making it safe "
        "to place inside a DAG."
    ),
    input_schema=_Inputs,
    output_schema=_Outputs,
    secrets=(),
    tags=("files", "watch", "poll", "storage"),
)


def _fingerprint(store: Any, tenant_id: str, prefix: str) -> dict[str, str]:
    """Map every key under `prefix` to a content fingerprint.

    Prefer the object's sha256 (via stat) so a same-size overwrite still
    registers as a modify; fall back to size when a digest is unavailable.
    """
    out: dict[str, str] = {}
    for obj in store.list(tenant_id, prefix):
        fp = obj.sha256
        if not fp:
            try:
                fp = store.stat(obj.uri).sha256
            except Exception:  # noqa: BLE001 - object vanished mid-listing; skip
                continue
        out[obj.key] = fp or f"size:{obj.size}"
    return out


def _diff(
    baseline: dict[str, str], current: dict[str, str]
) -> list[dict[str, str]]:
    """Compute create/modify/delete changes between two fingerprint maps."""
    changes: list[dict[str, str]] = []
    for key, fp in current.items():
        if key not in baseline:
            changes.append({"key": key, "kind": "create"})
        elif baseline[key] != fp:
            changes.append({"key": key, "kind": "modify"})
    for key in baseline:
        if key not in current:
            changes.append({"key": key, "kind": "delete"})
    changes.sort(key=lambda c: (c["key"], c["kind"]))
    return changes


async def handler(ctx: ActivityContext, inputs: dict[str, Any]) -> dict[str, Any]:
    prefix = inputs.get("prefix") or ""
    timeout_s = float(inputs.get("timeout_s", _DEFAULT_TIMEOUT_S))
    poll_ms = int(inputs.get("poll_ms", _DEFAULT_POLL_MS))
    poll_s = max(poll_ms, _MIN_POLL_MS) / 1000.0
    tenant_id = str(ctx.tenant_id)

    store = ctx.object_store
    if store is None:
        raise RuntimeError("cap.file_watch requires an object_store")

    logger.info(
        "cap.file_watch start run_id=%s prefix=%r timeout_s=%.3f poll_ms=%d",
        ctx.run_id,
        prefix,
        timeout_s,
        poll_ms,
    )

    baseline = _fingerprint(store, tenant_id, prefix)
    start = time.monotonic()
    deadline = start + timeout_s
    polls = 0

    while True:
        # Sleep first: a change present at t=0 isn't a "change" relative to
        # the baseline we just took, and we want to give writers a window.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(poll_s, remaining))
        polls += 1

        current = _fingerprint(store, tenant_id, prefix)
        changes = _diff(baseline, current)
        if changes:
            elapsed = time.monotonic() - start
            logger.info(
                "cap.file_watch detected run_id=%s prefix=%r changes=%d polls=%d "
                "elapsed_s=%.3f",
                ctx.run_id,
                prefix,
                len(changes),
                polls,
                elapsed,
            )
            return {
                "changes": changes,
                "changed": True,
                "polls": polls,
                "elapsed_s": round(elapsed, 3),
            }

    elapsed = time.monotonic() - start
    logger.info(
        "cap.file_watch timeout run_id=%s prefix=%r polls=%d elapsed_s=%.3f",
        ctx.run_id,
        prefix,
        polls,
        elapsed,
    )
    return {
        "changes": [],
        "changed": False,
        "polls": polls,
        "elapsed_s": round(elapsed, 3),
    }
