"""Definition types for the capability/action/control registry.

Every entry the LLM is allowed to reference must show up here. Capability
definitions are staff-authored (v1); actions are platform-provided primitives;
controls are flow-control nodes the interpreter understands directly.

Each definition declares its input and output shapes as Pydantic models. The
registry exposes those models to the validator (for shape-checking inputs) and
to the planner prompt builder (which serializes them as JSON Schema for the
LLM).

Capabilities additionally declare:
  - secrets: descriptors of credentials the worker will fetch from vault by
             alias at execution time. Names only — never values.
  - tags:    soft labels used for capability search / filtering.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel

from aakaar.shared.dag.types import NodeKind


@dataclass(frozen=True, slots=True)
class SecretSpec:
    """A required credential for a capability. The capability grant supplies a
    vault reference for each secret name; the worker fetches the actual value
    at execution time."""

    name: str  # e.g. "username", "password", "api_token"
    description: str = ""


@dataclass(frozen=True, slots=True)
class _BaseDefinition:
    ref: str
    description: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]


@dataclass(frozen=True, slots=True)
class CapabilityDefinition(_BaseDefinition):
    """A staff-authored, tenant-grantable macro (e.g. cap.hdfc_portal_login).

    Capabilities are the highest-leverage thing the LLM can reach for: they
    encapsulate site-specific automation behind a stable interface and own the
    credentials they need.
    """

    secrets: tuple[SecretSpec, ...] = field(default_factory=tuple)
    tags: tuple[str, ...] = field(default_factory=tuple)
    kind: NodeKind = field(default=NodeKind.CAPABILITY, init=False)


@dataclass(frozen=True, slots=True)
class ActionDefinition(_BaseDefinition):
    """A generic primitive (browser.navigate, http.request, file.parse_csv, …).

    Actions are platform-provided and tenant-agnostic — they don't carry
    credentials. If an action needs auth, that's a sign it should be wrapped
    in a capability instead.
    """

    tags: tuple[str, ...] = field(default_factory=tuple)
    kind: NodeKind = field(default=NodeKind.ACTION, init=False)


@dataclass(frozen=True, slots=True)
class ControlDefinition(_BaseDefinition):
    """A flow-control node the interpreter understands directly (branch,
    for_each, wait, human.prompt). Controls cannot be replaced by user code —
    the interpreter has special semantics for them."""

    kind: NodeKind = field(default=NodeKind.CONTROL, init=False)


Definition = CapabilityDefinition | ActionDefinition | ControlDefinition
