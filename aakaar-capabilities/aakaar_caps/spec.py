"""Declarative spec for a shared capability (host-agnostic)."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel


@dataclass(frozen=True)
class CapabilitySpec:
    ref: str
    description: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    version: str = "1"
    secrets: tuple[tuple[str, str], ...] = ()  # (name, description) pairs
    tags: tuple[str, ...] = ()
    gui: bool = False
    # Dry-run classification, surfaced to the executor's side-effect gate. None
    # = undeclared, which the executor treats as side-effecting (fail-safe: an
    # undeclared cap is simulated in a dry-run, never performed). False = proven
    # read-only (runs for real even in a dry-run).
    side_effecting: bool | None = None
    # True for caps that hold a live cross-node session (browser.open_session
    # and the flows built on it). Such caps are pinned to one agent for the
    # whole session and are NOT retried onto a fresh session — see the executor
    # session-affinity / retry rules.
    stateful_session: bool = False
