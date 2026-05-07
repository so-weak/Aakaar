"""Tests for the registry and its built-in primitives."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from aakar.shared.dag.types import NodeKind
from aakar.shared.registry import (
    ActionDefinition,
    CapabilityDefinition,
    Registry,
    RegistryConflict,
    SecretSpec,
    build_default_registry,
)


class _In(BaseModel):
    pass


class _Out(BaseModel):
    pass


def test_default_registry_has_expected_primitives() -> None:
    reg = build_default_registry()

    # A handful of refs the planner will lean on most.
    must_have = {
        "browser.open_session",
        "browser.navigate",
        "browser.download",
        "http.request",
        "file.parse_csv",
        "storage.put",
        "control.wait",
        "human.prompt",
    }
    refs = {d.ref for d in reg}
    missing = must_have - refs
    assert not missing, f"default registry missing: {missing}"


def test_default_registry_has_no_capabilities() -> None:
    """Capabilities are loaded by the capabilities loader, not the builtins."""
    reg = build_default_registry()
    assert reg.capabilities() == []


def test_kinds_are_correct() -> None:
    reg = build_default_registry()
    by_ref = {d.ref: d for d in reg}
    assert by_ref["browser.navigate"].kind is NodeKind.ACTION
    assert by_ref["control.wait"].kind is NodeKind.CONTROL
    assert by_ref["human.prompt"].kind is NodeKind.CONTROL


def test_registry_conflict() -> None:
    reg = Registry()
    a = ActionDefinition(
        ref="x.y",
        description="first",
        input_schema=_In,
        output_schema=_Out,
    )
    b = ActionDefinition(
        ref="x.y",
        description="second",
        input_schema=_In,
        output_schema=_Out,
    )
    reg.add(a)
    reg.add(a)  # idempotent on identity
    with pytest.raises(RegistryConflict):
        reg.add(b)


def test_capability_definition_carries_secrets() -> None:
    cap = CapabilityDefinition(
        ref="cap.demo",
        description="d",
        input_schema=_In,
        output_schema=_Out,
        secrets=(SecretSpec(name="username"), SecretSpec(name="password")),
        tags=("auth",),
    )
    assert cap.kind is NodeKind.CAPABILITY
    assert {s.name for s in cap.secrets} == {"username", "password"}
