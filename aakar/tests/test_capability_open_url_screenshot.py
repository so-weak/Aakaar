"""End-to-end tests for cap.open_url and cap.screenshot.

Exercises the hybrid pairing: cap.open_url is self-contained (opens a
session, navigates) and hands its session id to cap.screenshot, which
needs an upstream session. The chained DAG mirrors how the planner is
expected to compose them when the user asks for "a screenshot of <url>".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from aakar.capabilities import load_into
from aakar.capabilities.open_url import CAP_REF as OPEN_REF
from aakar.capabilities.screenshot import CAP_REF as SHOT_REF
from aakar.interpreter import LocalExecutor, RunContext, build_default_activities
from aakar.interpreter.activities.registry import ActivityRegistry
from aakar.interpreter.activities.types import ActivityContext
from aakar.interpreter.events import InMemoryEventRecorder
from aakar.interpreter.signals import SignalHub
from aakar.shared.dag.types import Dag, Edge, Node, NodeKind
from aakar.shared.registry import build_default_registry
from aakar.storage import LocalFsObjectStore
from aakar.vault import LocalVault
from aakar.workers.browser import FakeBrowserPool, FakeBrowserSession


@dataclass
class _Setup:
    actx: ActivityContext
    ctx: RunContext
    sess: FakeBrowserSession
    activities: ActivityRegistry


def _build(tmp_path: Path) -> _Setup:
    tenant_id = uuid.uuid4()
    sess = FakeBrowserSession()
    pool = FakeBrowserPool(next_sessions=[sess])
    registry = build_default_registry()
    activities = build_default_activities()
    load_into(registry, activities)
    actx = ActivityContext(
        tenant_id=tenant_id,
        run_id=uuid.uuid4(),
        registry=registry,
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=LocalVault(tmp_path / "vault"),
        browser_pool=pool,
        granted_capabilities={
            OPEN_REF: {"primary": {"vault_ref": "", "input_defaults": {}}},
            SHOT_REF: {"primary": {"vault_ref": "", "input_defaults": {}}},
        },
    )
    ctx = RunContext(run_id=actx.run_id, tenant_id=tenant_id, activity_ctx=actx)
    return _Setup(actx=actx, ctx=ctx, sess=sess, activities=activities)


def _executor(activities: ActivityRegistry) -> LocalExecutor:
    return LocalExecutor(
        activities=activities, recorder=InMemoryEventRecorder(), signals=SignalHub()
    )


@pytest.mark.asyncio
async def test_open_url_navigates_and_returns_session(tmp_path: Path) -> None:
    s = _build(tmp_path)
    dag = Dag(
        nodes=[
            Node(
                id="open", kind=NodeKind.CAPABILITY, ref=OPEN_REF,
                inputs={"url": "https://aakar.test/dashboard"},
            )
        ]
    )
    outcome = await _executor(s.activities).execute(dag, s.ctx)
    assert outcome.status == "succeeded", outcome.error
    assert outcome.outputs["open"] == {
        "session": s.sess.id,
        "url": "https://aakar.test/dashboard",
    }
    assert ("navigate", {"url": "https://aakar.test/dashboard"}) in s.sess.calls
    # No wait_selector was provided — the handler must not have called wait_for.
    assert not any(c[0] == "wait_for" for c in s.sess.calls)


@pytest.mark.asyncio
async def test_open_url_waits_for_selector(tmp_path: Path) -> None:
    s = _build(tmp_path)
    dag = Dag(
        nodes=[
            Node(
                id="open", kind=NodeKind.CAPABILITY, ref=OPEN_REF,
                inputs={
                    "url": "https://aakar.test/dashboard",
                    "wait_selector": "main[data-ready]",
                    "timeout_ms": 5000,
                },
            )
        ]
    )
    outcome = await _executor(s.activities).execute(dag, s.ctx)
    assert outcome.status == "succeeded", outcome.error
    waits = [c for c in s.sess.calls if c[0] == "wait_for"]
    assert len(waits) == 1
    assert waits[0][1]["selector"] == "main[data-ready]"
    assert waits[0][1]["timeout_ms"] == 5000


@pytest.mark.asyncio
async def test_open_url_then_screenshot_chain(tmp_path: Path) -> None:
    """Hybrid pairing: cap.open_url's session id flows into cap.screenshot."""
    s = _build(tmp_path)
    dag = Dag(
        nodes=[
            Node(
                id="open", kind=NodeKind.CAPABILITY, ref=OPEN_REF,
                inputs={"url": "https://aakar.test/report"},
                outputs_as="open",
            ),
            Node(
                id="shot", kind=NodeKind.CAPABILITY, ref=SHOT_REF,
                inputs={"session": "${open.session}"},
            ),
        ],
        edges=[Edge.model_validate({"from": "open", "to": "shot"})],
    )
    outcome = await _executor(s.activities).execute(dag, s.ctx)
    assert outcome.status == "succeeded", outcome.error

    image_uri = outcome.outputs["shot"]["image_uri"]
    assert image_uri.startswith("aakar://")
    assert image_uri.endswith(".png")
    assert s.actx.object_store.get(image_uri) == b"\x89PNG\r\n"

    # Full-page screenshot path (no selector) → screenshot(), not screenshot_element().
    assert any(c[0] == "screenshot" for c in s.sess.calls)
    assert not any(c[0] == "screenshot_element" for c in s.sess.calls)


@pytest.mark.asyncio
async def test_screenshot_element_scoped(tmp_path: Path) -> None:
    """When `selector` is set, the handler captures only that element and
    waits for it first (without needing an explicit `wait_selector`)."""
    s = _build(tmp_path)
    s.sess.element_screenshot_responses["#chart"] = b"\x89PNG\r\nchart-bytes"
    dag = Dag(
        nodes=[
            Node(
                id="open", kind=NodeKind.CAPABILITY, ref=OPEN_REF,
                inputs={"url": "https://aakar.test/report"},
                outputs_as="open",
            ),
            Node(
                id="shot", kind=NodeKind.CAPABILITY, ref=SHOT_REF,
                inputs={"session": "${open.session}", "selector": "#chart"},
            ),
        ],
        edges=[Edge.model_validate({"from": "open", "to": "shot"})],
    )
    outcome = await _executor(s.activities).execute(dag, s.ctx)
    assert outcome.status == "succeeded", outcome.error

    element_shots = [c for c in s.sess.calls if c[0] == "screenshot_element"]
    assert len(element_shots) == 1
    assert element_shots[0][1]["selector"] == "#chart"
    assert not any(c[0] == "screenshot" for c in s.sess.calls)
    waits = [c for c in s.sess.calls if c[0] == "wait_for" and c[1]["selector"] == "#chart"]
    assert waits, f"expected wait_for(#chart) before screenshot_element; got {s.sess.calls}"

    image_uri = outcome.outputs["shot"]["image_uri"]
    assert s.actx.object_store.get(image_uri) == b"\x89PNG\r\nchart-bytes"
