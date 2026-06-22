"""Portable LLM message types shared by the planner and capabilities.

These live in ``aakaar_caps`` (not the server package) so a capability that runs
on a remote agent can build the same message objects the server's planner
consumes. The server's ``aakaar.planner.llm`` re-exports ``Role``/``LLMMessage``
from here, so both hosts use one canonical type and a message built on the agent
is accepted by the server's ``complete_planner`` verbatim.

Only the host-agnostic pieces live here. ``PlannerCompletion`` deliberately
stays in the server's planner module because it depends on the server-only
``Dag`` type — and capabilities only ever need *free text* out of the LLM
(see ``CapabilityContext.complete_plan``), never the structured plan envelope.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class LLMMessage:
    role: Role
    content: str
