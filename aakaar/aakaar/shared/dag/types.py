"""DAG schema — the single source of truth for what an Aakaar workflow looks like.

A DAG is plain data. The interpreter walks it; the LLM produces it; nothing else
in the system has its own opinion about workflow shape.

Three node kinds:
  - capability: a tenant-granted, registry-defined macro (e.g. cap.hdfc_login)
  - action:     a generic primitive (e.g. browser.navigate, http.request)
  - control:    flow control (branch, for_each, wait, human.prompt)

Inputs may reference upstream node outputs via ${nodeId.field} strings. The DAG
itself never carries credentials or live data — only configuration and references.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

NODE_ID_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
REF_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
# Placement selector: "server" / unset = run on the API host (default);
# otherwise a remote-agent selector — an agent alias or a pool/label
# (e.g. "branch-ops", "pool:kiosk", "os:windows"). Resolved at run time
# against the live agent registry.
TARGET_RE = re.compile(r"^(server|[a-z][a-z0-9_:\-]{0,63})$")


class NodeKind(str, Enum):
    CAPABILITY = "capability"
    ACTION = "action"
    CONTROL = "control"


class RetrySpec(BaseModel):
    """Optional per-node retry policy. Absent => the node runs once (the
    historical behavior). Control nodes (human.prompt, control.wait) are never
    retried even if a policy is set — pausing for a human or sleeping is not a
    transient failure."""

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=1, ge=1, le=10)
    backoff_ms: int = Field(default=500, ge=0, le=60_000)


class Node(BaseModel):
    """A single step in a DAG.

    `ref` resolves through the registry: capability refs start with `cap.`,
    action refs are dotted (e.g. `browser.navigate`), control refs start with
    `control.` or `human.`.

    `outputs_as` is an optional alias for the node's outputs in the run env.
    When unset, references use the node's id (e.g. `${n3.pdf}`); when set,
    references use the alias (e.g. `${session.cookies}`).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: NodeKind
    ref: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs_as: str | None = None
    retry: RetrySpec | None = None
    target: str | None = None
    """Placement: where this node runs. None/"server" => the API host (default,
    historical behavior). Any other value is a remote-agent selector resolved at
    run time. Control nodes must run on the server."""

    @field_validator("target")
    @classmethod
    def _check_target(cls, v: str | None) -> str | None:
        if v is not None and not TARGET_RE.match(v):
            raise ValueError(f"invalid target {v!r}: must match {TARGET_RE.pattern}")
        return v

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not NODE_ID_RE.match(v):
            raise ValueError(f"invalid node id {v!r}: must match {NODE_ID_RE.pattern}")
        return v

    @field_validator("ref")
    @classmethod
    def _check_ref(cls, v: str) -> str:
        if not REF_RE.match(v):
            raise ValueError(f"invalid ref {v!r}: must match {REF_RE.pattern}")
        return v

    @field_validator("outputs_as")
    @classmethod
    def _check_outputs_as(cls, v: str | None) -> str | None:
        if v is not None and not NODE_ID_RE.match(v):
            raise ValueError(f"invalid outputs_as {v!r}: must match {NODE_ID_RE.pattern}")
        return v


class Edge(BaseModel):
    """A directed edge between two nodes. `from` is a Python keyword so we
    expose it via the `source` field with an alias."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source: str = Field(alias="from")
    target: str = Field(alias="to")


class Dag(BaseModel):
    """A workflow DAG. `id` and `version` are assigned by the workflow service;
    the planner emits them as zeros and the service overwrites on save."""

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    version: int = 0
    nodes: list[Node]
    edges: list[Edge] = Field(default_factory=list)
