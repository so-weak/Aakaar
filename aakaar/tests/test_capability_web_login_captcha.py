"""Captcha-aware path through cap.web_login.

The handler captures the captcha image via `screenshot_element`, opens a
SignalHub prompt, and waits for the user's response before submitting.
We drive that path here with a SignalHub we resolve from the test
side — exactly what `POST /runs/{id}/respond` does at runtime.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from aakaar.capabilities import load_into
from aakaar.capabilities.web_login import CAP_REF
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


@pytest.mark.asyncio
async def test_web_login_pauses_for_captcha_then_submits(tmp_path: Path) -> None:
    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    vault_ref = f"grants/{uuid.uuid4()}"
    vault.put(str(tenant_id), vault_ref, {"username": "u", "password": "p"})

    captcha_png = b"\x89PNG\r\n--captcha-bytes--"
    sess = FakeBrowserSession(
        element_screenshot_responses={"img.captcha": captcha_png},
        evaluate_responses=discovery_response(),
    )
    pool = FakeBrowserPool(next_sessions=[sess])

    registry = build_default_registry()
    activities = build_default_activities()
    load_into(registry, activities)

    object_store = LocalFsObjectStore(tmp_path / "objs")
    actx = ActivityContext(
        tenant_id=tenant_id,
        run_id=uuid.uuid4(),
        registry=registry,
        object_store=object_store,
        vault=vault,
        browser_pool=pool,
        granted_capabilities={
            CAP_REF: {"primary": {"vault_ref": vault_ref, "input_defaults": {}}},
        },
    )
    ctx = RunContext(run_id=actx.run_id, tenant_id=tenant_id, activity_ctx=actx)

    signals = SignalHub()
    executor = LocalExecutor(
        activities=activities, recorder=InMemoryEventRecorder(), signals=signals
    )

    dag = Dag(
        nodes=[
            Node(
                id="login",
                kind=NodeKind.CAPABILITY,
                ref=CAP_REF,
                inputs={
                    "account_alias": "primary",
                    "login_url": "https://app.payops.test/login",
                    "captcha_image_selector": "img.captcha",
                    "captcha_input_selector": "input[name='captcha']",
                    "success_selector": "nav.account",
                },
            )
        ]
    )

    # Drive the executor and the captcha responder concurrently. The
    # capability will pause inside fetch→fill→fill→<wait_for_captcha>; we
    # poll for the pending prompt and resolve it.
    async def respond_to_captcha() -> None:
        for _ in range(50):
            pending = signals.list_pending(actx.run_id)
            if pending:
                await signals.resolve(
                    run_id=actx.run_id,
                    node_id=pending[0].node_id,
                    response="A1B2",
                )
                return
            await asyncio.sleep(0.01)
        raise AssertionError("captcha prompt never opened")

    outcome, _ = await asyncio.gather(
        executor.execute(dag, ctx),
        respond_to_captcha(),
    )
    assert outcome.status == "succeeded", outcome.error

    # Captcha image was captured and persisted.
    element_shots = [c for c in sess.calls if c[0] == "screenshot_element"]
    assert len(element_shots) == 1
    assert element_shots[0][1]["selector"] == "img.captcha"

    # Browser was driven in the right order: nav, evaluate (discovery),
    # wait, fill x2, wait_captcha, screenshot_element, fill_captcha, click,
    # wait_success.
    kinds = [c[0] for c in sess.calls]
    assert kinds == [
        "navigate",
        "evaluate",  # login-form discovery
        "wait_for",  # username field
        "fill",  # username
        "fill",  # password
        "wait_for",  # captcha image
        "screenshot_element",
        "fill",  # captcha value
        "click",  # submit
        "wait_for",  # success_selector
    ]
    fills = [c[1] for c in sess.calls if c[0] == "fill"]
    captcha_fill = fills[2]
    assert captcha_fill["selector"] == "input[name='captcha']"
    assert captcha_fill["value"] == "A1B2"

    # The captcha image lives in managed storage at the expected key shape.
    # (We can't trivially reverse the URI back to bytes without the object
    # store reader; but we exercise that round-trip in the file_download
    # test. Here the assertion that screenshot_element ran is sufficient.)


def test_inputs_reject_partial_captcha_pair() -> None:
    from aakaar_caps.caps.web_login import _Inputs

    with pytest.raises(ValueError):
        _Inputs(
            account_alias="primary",
            login_url="https://example.test/login",
            captcha_image_selector="img.captcha",
            # missing captcha_input_selector
        )


@pytest.mark.asyncio
async def test_captcha_handler_refuses_without_signals(tmp_path: Path) -> None:
    """If the cap is invoked without a HITL channel (no signal_opener), it must
    refuse rather than hang silently. The executor/agent always wire one; this
    guards against test or CLI invocations that bypass it."""
    from aakaar_caps.caps.web_login import run
    from aakaar_caps.context import CapabilityContext, CapabilityError

    sess = FakeBrowserSession(
        element_screenshot_responses={"img.captcha": b"x"},
        evaluate_responses=discovery_response(),
    )
    pool = FakeBrowserPool(next_sessions=[sess])

    async def _writer(_key: str, _data: bytes) -> str:
        return "aakaar://t/x/captcha.png"

    ctx = CapabilityContext(
        secrets={"username": "u", "password": "p"},
        run_id=str(uuid.uuid4()),
        node_id="login",
        tenant_id="t",
        browser_pool=pool,
        object_writer=_writer,
        signal_opener=None,  # deliberately no HITL channel
    )

    with pytest.raises(CapabilityError, match="human-in-the-loop"):
        await run(
            ctx,
            {
                "account_alias": "primary",
                "login_url": "https://example.test/login",
                "captcha_image_selector": "img.captcha",
                "captcha_input_selector": "input[name='captcha']",
            },
        )
