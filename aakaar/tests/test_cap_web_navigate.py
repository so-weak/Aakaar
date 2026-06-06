"""End-to-end tests for cap.web_navigate.

Exercises the two modes (fresh session vs. reused session) and the step
loop (goto / wait_for / click) against the FakeBrowserPool, plus the
auto-registration into the default registry. final_url / title are read
via session.evaluate(), so the fake is primed with evaluate_responses.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from aakaar.capabilities import load_into
from aakaar.capabilities.open_url import CAP_REF as OPEN_REF
from aakaar.capabilities.web.web_navigate import CAP_REF as NAV_REF
from aakaar.interpreter import LocalExecutor, RunContext, build_default_activities
from aakaar.interpreter.activities.registry import ActivityRegistry
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.interpreter.events import InMemoryEventRecorder
from aakaar.interpreter.signals import SignalHub
from aakaar.shared.dag.types import Dag, Edge, Node, NodeKind
from aakaar.shared.registry import build_default_registry
from aakaar.storage import LocalFsObjectStore
from aakaar.vault import LocalVault
from aakaar.workers.browser import FakeBrowserPool, FakeBrowserSession


@dataclass
class _Setup:
    actx: ActivityContext
    ctx: RunContext
    sess: FakeBrowserSession
    activities: ActivityRegistry


def _build(tmp_path: Path) -> _Setup:
    tenant_id = uuid.uuid4()
    sess = FakeBrowserSession()
    # Prime the JS reads cap.web_navigate uses to populate final_url/title.
    sess.evaluate_responses["window.location.href"] = "https://aakaar.test/final"
    sess.evaluate_responses["document.title"] = "Aakaar Report"
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
            NAV_REF: {"primary": {"vault_ref": "", "input_defaults": {}}},
            OPEN_REF: {"primary": {"vault_ref": "", "input_defaults": {}}},
        },
    )
    ctx = RunContext(run_id=actx.run_id, tenant_id=tenant_id, activity_ctx=actx)
    return _Setup(actx=actx, ctx=ctx, sess=sess, activities=activities)


def _executor(activities: ActivityRegistry) -> LocalExecutor:
    return LocalExecutor(
        activities=activities, recorder=InMemoryEventRecorder(), signals=SignalHub()
    )


def test_registered_in_default_registry() -> None:
    registry = build_default_registry()
    activities = build_default_activities()
    load_into(registry, activities)
    assert registry.get(NAV_REF) is not None
    assert NAV_REF == "cap.web_navigate"


@pytest.mark.asyncio
async def test_fresh_session_runs_steps_and_returns_state(tmp_path: Path) -> None:
    s = _build(tmp_path)
    dag = Dag(
        nodes=[
            Node(
                id="nav",
                kind=NodeKind.CAPABILITY,
                ref=NAV_REF,
                inputs={
                    "url": "https://aakaar.test/start",
                    "steps": [
                        {"action": "wait_for", "value": "main[data-ready]"},
                        {"action": "click", "value": "a.report-link"},
                        {"action": "goto", "value": "https://aakaar.test/report"},
                    ],
                    "wait_timeout_ms": 5000,
                },
            )
        ]
    )
    outcome = await _executor(s.activities).execute(dag, s.ctx)
    assert outcome.status == "succeeded", outcome.error

    out = outcome.outputs["nav"]
    assert out["session_id"] == s.sess.id
    assert out["final_url"] == "https://aakaar.test/final"
    assert out["title"] == "Aakaar Report"

    # First call is the initial navigate, then the steps in order.
    nav_calls = [c for c in s.sess.calls if c[0] == "navigate"]
    assert nav_calls[0] == ("navigate", {"url": "https://aakaar.test/start"})
    assert nav_calls[1] == ("navigate", {"url": "https://aakaar.test/report"})

    waits = [c for c in s.sess.calls if c[0] == "wait_for"]
    assert len(waits) == 1
    assert waits[0][1]["selector"] == "main[data-ready]"
    assert waits[0][1]["timeout_ms"] == 5000

    clicks = [c for c in s.sess.calls if c[0] == "click"]
    assert clicks == [("click", {"selector": "a.report-link"})]

    # The session must be reachable for downstream nodes.
    holder = s.actx.session_state.get(f"browser:{s.sess.id}")
    assert holder is not None
    assert holder.session is s.sess


@pytest.mark.asyncio
async def test_no_steps_just_navigates(tmp_path: Path) -> None:
    s = _build(tmp_path)
    dag = Dag(
        nodes=[
            Node(
                id="nav",
                kind=NodeKind.CAPABILITY,
                ref=NAV_REF,
                inputs={"url": "https://aakaar.test/only"},
            )
        ]
    )
    outcome = await _executor(s.activities).execute(dag, s.ctx)
    assert outcome.status == "succeeded", outcome.error
    assert outcome.outputs["nav"]["session_id"] == s.sess.id
    assert ("navigate", {"url": "https://aakaar.test/only"}) in s.sess.calls
    assert not any(c[0] in ("wait_for", "click") for c in s.sess.calls)


@pytest.mark.asyncio
async def test_reuses_existing_session_from_open_url(tmp_path: Path) -> None:
    """cap.open_url opens a session; cap.web_navigate reuses it by id and
    must NOT check out a second session from the pool."""
    s = _build(tmp_path)
    dag = Dag(
        nodes=[
            Node(
                id="open",
                kind=NodeKind.CAPABILITY,
                ref=OPEN_REF,
                inputs={"url": "https://aakaar.test/login"},
                outputs_as="open",
            ),
            Node(
                id="nav",
                kind=NodeKind.CAPABILITY,
                ref=NAV_REF,
                inputs={
                    "url": "https://aakaar.test/dashboard",
                    "session_id": "${open.session}",
                    "steps": [{"action": "click", "value": "#menu"}],
                },
            ),
        ],
        edges=[Edge.model_validate({"from": "open", "to": "nav"})],
    )
    outcome = await _executor(s.activities).execute(dag, s.ctx)
    assert outcome.status == "succeeded", outcome.error

    # Exactly one session handed out for the whole run (reuse worked).
    assert len(s.actx.session_state.values()) == 1
    assert s.sess in s.actx.browser_pool.handed_out
    assert len(s.actx.browser_pool.handed_out) == 1

    assert outcome.outputs["nav"]["session_id"] == s.sess.id
    assert ("navigate", {"url": "https://aakaar.test/dashboard"}) in s.sess.calls
    assert ("click", {"selector": "#menu"}) in s.sess.calls


@pytest.mark.asyncio
async def test_unknown_session_id_fails_cleanly(tmp_path: Path) -> None:
    s = _build(tmp_path)
    dag = Dag(
        nodes=[
            Node(
                id="nav",
                kind=NodeKind.CAPABILITY,
                ref=NAV_REF,
                inputs={
                    "url": "https://aakaar.test/x",
                    "session_id": "does-not-exist",
                },
            )
        ]
    )
    outcome = await _executor(s.activities).execute(dag, s.ctx)
    assert outcome.status == "failed"
    assert "no live browser session" in (outcome.error or {}).get("message", "")
    # No fresh session should have been checked out on the reuse path.
    assert s.actx.browser_pool.handed_out == []


def test_input_schema_rejects_unknown_step_action() -> None:
    from pydantic import ValidationError

    from aakaar.capabilities.web.web_navigate import _Inputs

    with pytest.raises(ValidationError):
        _Inputs.model_validate(
            {"url": "https://x", "steps": [{"action": "scroll", "value": "y"}]}
        )
    # Extra top-level fields are forbidden too.
    with pytest.raises(ValidationError):
        _Inputs.model_validate({"url": "https://x", "bogus": 1})
