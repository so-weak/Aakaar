"""ActivityContext — what each activity sees at runtime.

The context carries:
  - tenant + run identity (for storage/vault scoping, audit trails)
  - object store, vault, registry references
  - a per-run state dict (`session_state`) where activities can stash live
    handles between calls — e.g. a browser session opened by browser.open_session
    and consumed by browser.navigate

`session_state` is what makes the executor stateful within a run. Refs in
the DAG carry *strings* (handles), and the resolver looks up the actual
live object (BrowserSession, file handle, etc.) from session_state.

This pattern keeps the DAG JSON small and serializable while letting
multi-step browser flows share a Playwright context.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from aakar.shared.registry import Registry
from aakar.storage.object_store import ObjectStorage
from aakar.vault import Vault


@dataclass
class ActivityContext:
    tenant_id: uuid.UUID
    run_id: uuid.UUID
    registry: Registry
    object_store: ObjectStorage
    vault: Vault
    session_state: dict[str, Any] = field(default_factory=dict)
    granted_capabilities: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Map of capability_ref -> { account_alias -> {vault_ref, input_defaults} }.
    Populated by the orchestrator from the DB at run start."""

    # Optional — set when the deployment includes a browser worker.
    browser_pool: Any = None
    """A `BrowserPool` (workers.browser.session). Optional so non-browser
    deployments don't pay the import or runtime cost."""

    # Set per dispatch by the executor so capability/action handlers can
    # pause for human input mid-flow (e.g. captchas, OTPs). The handler
    # uses `signals.open(run_id=..., node_id=ctx.node_id, ...)` and awaits.
    signals: Any = None
    """A `SignalHub` from `aakar.interpreter.signals`. Set by the executor
    per run; None outside an active run (e.g. unit tests that drive
    handlers directly without HITL)."""
    node_id: str = ""
    """The DAG node currently being executed. The executor populates this
    via `dataclasses.replace` per dispatch so parallel nodes don't clobber
    each other. Empty string outside an active node."""
