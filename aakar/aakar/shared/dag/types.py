"""DAG schema — the single source of truth for what an Aakar workflow looks like.

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


class NodeKind(str, Enum):
    CAPABILITY = "capability"
    ACTION = "action"
    CONTROL = "control"


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
