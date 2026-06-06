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
