"""Tests for cap.web_login auto-discovery (DOM heuristics + LLM fallback).

Covers:
  - explicit user-supplied selectors skip discovery entirely
  - auto-discovery feeds selectors when the planner left them blank
  - auto-detected image captcha triggers HITL pause without the user
    having to specify selectors
  - 3rd-party captchas (recaptcha / hcaptcha / turnstile) trigger an
    HITL "confirm" pause
  - ambiguity_reasons + LLM fallback rewrites selectors when an LLM
    is wired into ActivityContext
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from aakaar_caps.caps.web_login import _llm_disambiguate
from aakaar_caps.context import CapabilityContext

from aakaar.capabilities import load_into
from aakaar.capabilities.web_login import CAP_REF
from aakaar.capabilities.web_login.discovery import LoginFormDescriptor
from aakaar.interpreter import LocalExecutor, RunContext, build_default_activities
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.interpreter.events import InMemoryEventRecorder
from aakaar.interpreter.signals import SignalHub
from aakaar.shared.dag.types import Dag, Node, NodeKind
from aakaar.shared.registry import build_default_registry
from aakaar.storage import LocalFsObjectStore
from aakaar.vault import LocalVault
from aakaar.workers.browser import FakeBrowserPool, FakeBrowserSession
from tests._discovery_helpers import discovery_response


def _setup(
    tmp_path: Path,
    *,
    pool: FakeBrowserPool,
    vault: LocalVault,
    granted: dict[str, Any],
    tenant_id: uuid.UUID,
    llm: Any = None,
) -> tuple[ActivityContext, LocalExecutor, SignalHub]:
    """Build a registry+activities pair with capabilities loaded, then
    construct an ActivityContext + LocalExecutor that share them."""
    registry = build_default_registry()
    activities = build_default_activities()
    load_into(registry, activities)
    actx = ActivityContext(
        tenant_id=tenant_id,
        run_id=uuid.uuid4(),
        registry=registry,
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=vault,
        browser_pool=pool,
        granted_capabilities=granted,
    )
    signals = SignalHub()
    executor = LocalExecutor(
        activities=activities,
        recorder=InMemoryEventRecorder(),
        signals=signals,
        llm=llm,
    )
    return actx, executor, signals


@pytest.mark.asyncio
async def test_explicit_selectors_skip_discovery(tmp_path: Path) -> None:
    """If the caller supplies username/password/submit, the handler must
    NOT call evaluate() — discovery is skipped entirely."""
    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    vault_ref = f"grants/{uuid.uuid4()}"
    vault.put(str(tenant_id), vault_ref, {"username": "u", "password": "p"})

    sess = FakeBrowserSession()
    pool = FakeBrowserPool(next_sessions=[sess])
    actx, executor, signals = _setup(
        tmp_path,
        pool=pool,
        vault=vault,
        granted={CAP_REF: {"primary": {"vault_ref": vault_ref, "input_defaults": {}}}},
        tenant_id=tenant_id,
    )
    ctx = RunContext(run_id=actx.run_id, tenant_id=tenant_id, activity_ctx=actx)

    dag = Dag(
        nodes=[
            Node(
                id="login",
                kind=NodeKind.CAPABILITY,
                ref=CAP_REF,
                inputs={
                    "account_alias": "primary",
                    "login_url": "https://example.test/login",
                    "username_selector": "#u",
                    "password_selector": "#p",
                    "submit_selector": "#go",
                },
            )
        ]
    )
    outcome = await executor.execute(dag, ctx)
    assert outcome.status == "succeeded", outcome.error
    assert all(c[0] != "evaluate" for c in sess.calls), (
        "discovery JS must not run when all three core selectors are supplied"
    )


@pytest.mark.asyncio
async def test_discovery_supplies_missing_selectors(tmp_path: Path) -> None:
    """No selectors provided → discovery returns them and login proceeds."""
    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    vault_ref = f"grants/{uuid.uuid4()}"
    vault.put(str(tenant_id), vault_ref, {"username": "u", "password": "p"})

    sess = FakeBrowserSession(
        evaluate_responses=discovery_response(
            username="#email",
            password="#password",
            submit="button.signin",
        ),
    )
    pool = FakeBrowserPool(next_sessions=[sess])
    actx, executor, signals = _setup(
        tmp_path,
        pool=pool,
        vault=vault,
        granted={CAP_REF: {"primary": {"vault_ref": vault_ref, "input_defaults": {}}}},
        tenant_id=tenant_id,
    )
    ctx = RunContext(run_id=actx.run_id, tenant_id=tenant_id, activity_ctx=actx)

    dag = Dag(
        nodes=[
            Node(
                id="login",
                kind=NodeKind.CAPABILITY,
                ref=CAP_REF,
                inputs={
                    "account_alias": "primary",
                    "login_url": "https://example.test/login",
                },
            )
        ]
    )
    outcome = await executor.execute(dag, ctx)
    assert outcome.status == "succeeded", outcome.error
    fills = [c for c in sess.calls if c[0] == "fill"]
    assert {(c[1]["selector"], c[1]["value"]) for c in fills} == {
        ("#email", "u"),
        ("#password", "p"),
    }
    clicks = [c for c in sess.calls if c[0] == "click"]
    assert clicks and clicks[0][1]["selector"] == "button.signin"


@pytest.mark.asyncio
async def test_discovery_auto_handles_image_captcha(tmp_path: Path) -> None:
    """User did NOT mention a captcha. Discovery surfaces one. Handler
    captures the image, opens the HITL signal, and continues — without
    the user ever knowing in advance there was a captcha."""
    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    vault_ref = f"grants/{uuid.uuid4()}"
    vault.put(str(tenant_id), vault_ref, {"username": "u", "password": "p"})

    captcha_png = b"\x89PNG-bytes"
    sess = FakeBrowserSession(
        element_screenshot_responses={"img.captcha": captcha_png},
        evaluate_responses=discovery_response(
            captcha_image="img.captcha",
            captcha_input="input[name='captcha']",
            captcha_kind="image",
        ),
    )
    pool = FakeBrowserPool(next_sessions=[sess])
    actx, executor, signals = _setup(
        tmp_path,
        pool=pool,
        vault=vault,
        granted={CAP_REF: {"primary": {"vault_ref": vault_ref, "input_defaults": {}}}},
        tenant_id=tenant_id,
    )
    ctx = RunContext(run_id=actx.run_id, tenant_id=tenant_id, activity_ctx=actx)

    dag = Dag(
        nodes=[
            Node(
                id="login",
                kind=NodeKind.CAPABILITY,
                ref=CAP_REF,
                inputs={
                    "account_alias": "primary",
                    "login_url": "https://example.test/login",
                },
            )
        ]
    )

    async def respond_to_captcha() -> None:
        for _ in range(50):
            pending = signals.list_pending(actx.run_id)
            if pending:
                await signals.resolve(
                    run_id=actx.run_id,
                    node_id=pending[0].node_id,
                    response="ABCD",
                )
                return
            await asyncio.sleep(0.01)
        raise AssertionError("captcha prompt never opened")

    outcome, _resp = await asyncio.gather(executor.execute(dag, ctx), respond_to_captcha())
    assert outcome.status == "succeeded", outcome.error

    # Captcha was actually screenshot'd and filled with the user's response.
    shots = [c for c in sess.calls if c[0] == "screenshot_element"]
    assert shots and shots[0][1]["selector"] == "img.captcha"
    captcha_fill = [
        c for c in sess.calls
        if c[0] == "fill" and c[1]["selector"] == "input[name='captcha']"
    ]
    assert captcha_fill and captcha_fill[0][1]["value"] == "ABCD"


@pytest.mark.asyncio
async def test_discovery_recaptcha_pauses_for_human_confirm(tmp_path: Path) -> None:
    """A reCAPTCHA / hCaptcha / Turnstile widget can't be solved with a
    text response. The handler should pause for a 'confirm' signal so the
    user can solve it in the (headed) browser, then continue."""
    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    vault_ref = f"grants/{uuid.uuid4()}"
    vault.put(str(tenant_id), vault_ref, {"username": "u", "password": "p"})

    sess = FakeBrowserSession(
        evaluate_responses=discovery_response(captcha_kind="recaptcha"),
    )
    pool = FakeBrowserPool(next_sessions=[sess])
    actx, executor, signals = _setup(
        tmp_path,
        pool=pool,
        vault=vault,
        granted={CAP_REF: {"primary": {"vault_ref": vault_ref, "input_defaults": {}}}},
        tenant_id=tenant_id,
    )
    ctx = RunContext(run_id=actx.run_id, tenant_id=tenant_id, activity_ctx=actx)

    dag = Dag(
        nodes=[
            Node(
                id="login",
                kind=NodeKind.CAPABILITY,
                ref=CAP_REF,
                inputs={
                    "account_alias": "primary",
                    "login_url": "https://example.test/login",
                },
            )
        ]
    )

    async def confirm() -> None:
        for _ in range(50):
            pending = signals.list_pending(actx.run_id)
            if pending:
                assert pending[0].expects == "confirm"
                assert "recaptcha" in pending[0].message.lower()
                await signals.resolve(
                    run_id=actx.run_id,
                    node_id=pending[0].node_id,
                    response="done",
                )
                return
            await asyncio.sleep(0.01)
        raise AssertionError("recaptcha confirm prompt never opened")

    outcome, _ = await asyncio.gather(executor.execute(dag, ctx), confirm())
    assert outcome.status == "succeeded", outcome.error
    # No screenshot call — we don't try to capture a recaptcha widget.
    assert all(c[0] != "screenshot_element" for c in sess.calls)


@pytest.mark.asyncio
async def test_llm_fallback_disambiguates(tmp_path: Path) -> None:
    """When discovery returns ambiguity_reasons and an LLM is wired,
    the handler asks the LLM for the right selectors and uses them."""

    class _ScriptedLLM:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        def complete_text(self, system, user):  # noqa: D401, ANN001
            # web_login disambiguation rides the free-text seam: the model
            # replies with the bare selector JSON (no PlannerCompletion
            # envelope). This is the reply the real proxy LLM produces.
            self.calls.append((system, user))
            return (
                '{"username_selector":"#user-overridden",'
                '"password_selector":"#pwd-overridden",'
                '"submit_selector":"button.go-overridden",'
                '"captcha_image_selector":null,'
                '"captcha_input_selector":null}'
            )

        def complete_planner(self, messages):  # noqa: D401, ANN001
            # web_login must NOT use the planner seam — its PlannerCompletion
            # envelope (requires `kind`, forbids bare selector keys) rejects
            # the reply, which is the bug this fix removes.
            raise AssertionError(
                "web_login disambiguation must use the free-text seam, "
                "not the planner envelope"
            )

    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    vault_ref = f"grants/{uuid.uuid4()}"
    vault.put(str(tenant_id), vault_ref, {"username": "u", "password": "p"})

    # Discovery returns selectors but flags ambiguity, triggering the
    # LLM fallback path.
    sess = FakeBrowserSession(
        evaluate_responses=discovery_response(
            username="#guess-u",
            password="#guess-p",
            submit="#guess-go",
            ambiguity_reasons=["multiple_text_inputs_before_password"],
            form_html="<form><input id='guess-u'/><input id='guess-p' type='password'/></form>",
        ),
    )
    pool = FakeBrowserPool(next_sessions=[sess])
    llm = _ScriptedLLM()
    actx, executor, signals = _setup(
        tmp_path,
        pool=pool,
        vault=vault,
        granted={CAP_REF: {"primary": {"vault_ref": vault_ref, "input_defaults": {}}}},
        tenant_id=tenant_id,
        llm=llm,
    )
    ctx = RunContext(run_id=actx.run_id, tenant_id=tenant_id, activity_ctx=actx)

    dag = Dag(
        nodes=[
            Node(
                id="login",
                kind=NodeKind.CAPABILITY,
                ref=CAP_REF,
                inputs={
                    "account_alias": "primary",
                    "login_url": "https://example.test/login",
                },
            )
        ]
    )
    outcome = await executor.execute(dag, ctx)
    assert outcome.status == "succeeded", outcome.error
    assert llm.calls, "LLM should have been called for disambiguation"
    fills = {(c[1]["selector"], c[1]["value"]) for c in sess.calls if c[0] == "fill"}
    assert ("#user-overridden", "u") in fills
    assert ("#pwd-overridden", "p") in fills
    clicks = [c for c in sess.calls if c[0] == "click"]
    assert clicks and clicks[0][1]["selector"] == "button.go-overridden"


@pytest.mark.asyncio
async def test_llm_fallback_silently_falls_back_when_no_llm(tmp_path: Path) -> None:
    """If discovery is ambiguous and no LLM is wired, the handler proceeds
    with whatever heuristics produced (no crash, no hang)."""
    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    vault_ref = f"grants/{uuid.uuid4()}"
    vault.put(str(tenant_id), vault_ref, {"username": "u", "password": "p"})
    sess = FakeBrowserSession(
        evaluate_responses=discovery_response(
            ambiguity_reasons=["multiple_text_inputs_before_password"],
        ),
    )
    pool = FakeBrowserPool(next_sessions=[sess])
    actx, executor, signals = _setup(
        tmp_path,
        pool=pool,
        vault=vault,
        granted={CAP_REF: {"primary": {"vault_ref": vault_ref, "input_defaults": {}}}},
        tenant_id=tenant_id,
         llm=None
    )
    ctx = RunContext(run_id=actx.run_id, tenant_id=tenant_id, activity_ctx=actx)

    dag = Dag(
        nodes=[
            Node(
                id="login",
                kind=NodeKind.CAPABILITY,
                ref=CAP_REF,
                inputs={
                    "account_alias": "primary",
                    "login_url": "https://example.test/login",
                },
            )
        ]
    )
    outcome = await executor.execute(dag, ctx)
    assert outcome.status == "succeeded", outcome.error


@pytest.mark.asyncio
async def test_discovery_failure_surfaces_clearly(tmp_path: Path) -> None:
    """If the page has no password input, the handler must fail with a
    clear error rather than driving phantom selectors."""
    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    vault_ref = f"grants/{uuid.uuid4()}"
    vault.put(str(tenant_id), vault_ref, {"username": "u", "password": "p"})

    sess = FakeBrowserSession(
        evaluate_responses={
            # The discovery JS returns ok=False with a no-password reason.
            "bestSelector": {
                "ok": False,
                "ambiguity_reasons": ["no_password_input"],
                "username_selector": None,
                "password_selector": None,
                "submit_selector": None,
                "captcha_image_selector": None,
                "captcha_input_selector": None,
                "captcha_kind": None,
                "form_outer_html_excerpt": "",
            }
        },
    )
    pool = FakeBrowserPool(next_sessions=[sess])
    actx, executor, signals = _setup(
        tmp_path,
        pool=pool,
        vault=vault,
        granted={CAP_REF: {"primary": {"vault_ref": vault_ref, "input_defaults": {}}}},
        tenant_id=tenant_id,
    )
    ctx = RunContext(run_id=actx.run_id, tenant_id=tenant_id, activity_ctx=actx)

    dag = Dag(
        nodes=[
            Node(
                id="login",
                kind=NodeKind.CAPABILITY,
                ref=CAP_REF,
                inputs={
                    "account_alias": "primary",
                    "login_url": "https://example.test/login",
                },
            )
        ]
    )
    outcome = await executor.execute(dag, ctx)
    assert outcome.status == "failed"
    assert "could not find a login form" in (outcome.error or {}).get("message", "")


def test_login_form_descriptor_serialization_roundtrip() -> None:
    """The descriptor should tolerate sparse / malformed JS results."""
    d = LoginFormDescriptor.from_js_result({"ok": True, "username_selector": "#u"})
    assert d.username_selector == "#u"
    assert d.password_selector is None
    assert d.has_captcha is False

    d_bad = LoginFormDescriptor.from_js_result(None)
    assert d_bad.ok is False
    assert "js_returned_non_object" in d_bad.ambiguity_reasons


@pytest.mark.asyncio
async def test_llm_disambiguate_handles_garbage_response(tmp_path: Path) -> None:
    """If the LLM replies with non-JSON via the free-text seam, the helper
    returns None — the handler then falls back to whatever heuristics produced."""
    # The portable text seam returns free text; garbage in, None out.
    ctx = CapabilityContext(text_completer=lambda _system, _user: "this is not json")
    desc = LoginFormDescriptor(
        ok=True,
        ambiguity_reasons=["x"],
        username_selector="#u",
        password_selector="#p",
        submit_selector="#go",
        captcha_image_selector=None,
        captcha_input_selector=None,
        captcha_kind=None,
        form_outer_html_excerpt="<form>...</form>",
    )
    refined = await _llm_disambiguate(ctx, desc)
    assert refined is None
