"""Tests for the system-prompt assembler.

These check the *content* of the prompt — not the LLM's behavior. The
critical invariants the planner depends on:

  - Only granted capabilities are listed.
  - Forbidden behaviors (asking for credentials, inventing refs) are
    explicitly stated.
  - Every action and control primitive in the registry is present.
  - The repair-feedback message round-trips errors faithfully.
"""

from __future__ import annotations

from pydantic import BaseModel

from aakaar.planner.prompt import PromptBuilder, _repair_message
from aakaar.shared.dag.types import Dag, Node, NodeKind
from aakaar.shared.registry import (
    CapabilityDefinition,
    Registry,
    SecretSpec,
    build_default_registry,
)


class _In(BaseModel):
    pass


class _Out(BaseModel):
    pass


def _registry_with_caps() -> Registry:
    reg = build_default_registry()
    reg.add(
        CapabilityDefinition(
            ref="cap.hdfc_login",
            description="Log into HDFC portal using stored credentials.",
            input_schema=_In,
            output_schema=_Out,
            secrets=(SecretSpec(name="username"), SecretSpec(name="password")),
            tags=("auth", "hdfc"),
        )
    )
    reg.add(
        CapabilityDefinition(
            ref="cap.icici_login",
            description="Log into ICICI portal.",
            input_schema=_In,
            output_schema=_Out,
        )
    )
    return reg


def test_only_granted_caps_appear() -> None:
    reg = _registry_with_caps()
    builder = PromptBuilder(registry=reg, granted_capabilities={"cap.hdfc_login"})
    msgs = builder.build_messages(user_message="login to hdfc")
    system = msgs[0].content
    assert "cap.hdfc_login" in system
    assert "cap.icici_login" not in system


def test_no_caps_emits_explicit_block() -> None:
    reg = _registry_with_caps()
    builder = PromptBuilder(registry=reg, granted_capabilities=set())
    system = builder.build_messages(user_message="anything")[0].content
    assert "No capabilities are granted" in system
    assert "missing" in system.lower()


def test_credential_prompt_forbidden_in_rules() -> None:
    reg = build_default_registry()
    builder = PromptBuilder(registry=reg, granted_capabilities=set())
    system = builder.build_messages(user_message="x")[0].content
    assert "NEVER ask for usernames" in system
    assert "passwords" in system.lower()
    assert "OTP" in system or "OTPs" in system


def test_secrets_listed_for_granted_capabilities() -> None:
    reg = _registry_with_caps()
    builder = PromptBuilder(registry=reg, granted_capabilities={"cap.hdfc_login"})
    system = builder.build_messages(user_message="x")[0].content
    assert "username" in system and "password" in system
    assert "auto-injected" in system


def test_actions_and_controls_present() -> None:
    reg = build_default_registry()
    builder = PromptBuilder(registry=reg, granted_capabilities=set())
    system = builder.build_messages(user_message="x")[0].content
    assert "browser.navigate" in system
    assert "browser.download" in system
    assert "control.wait" in system
    assert "human.prompt" in system


def test_user_message_is_appended() -> None:
    reg = build_default_registry()
    builder = PromptBuilder(registry=reg, granted_capabilities=set())
    msgs = builder.build_messages(user_message="please download my may statement")
    assert msgs[-1].content == "please download my may statement"


def test_current_dag_block_present_when_editing() -> None:
    reg = build_default_registry()
    builder = PromptBuilder(registry=reg, granted_capabilities=set())
    dag = Dag(nodes=[Node(id="a", kind=NodeKind.ACTION, ref="browser.open_session")])
    system = builder.build_messages(user_message="add a download step", current_dag=dag)[0].content
    assert "Current workflow" in system
    assert "browser.open_session" in system


def test_repair_message_lists_errors() -> None:
    msg = _repair_message(["unknown ref bogus.thing", "missing required input url"])
    assert "did not validate" in msg
    assert "unknown ref bogus.thing" in msg
    assert "missing required input url" in msg


def test_repair_path_appends_error_message() -> None:
    reg = build_default_registry()
    builder = PromptBuilder(registry=reg, granted_capabilities=set())
    msgs = builder.build_messages(
        user_message="x", repair_errors=["something broke"]
    )
    assert any("something broke" in m.content for m in msgs)
