"""End-to-end tests for cap.web_form_fill.

Covers both entry modes (fresh `url` session and reused `session_id`),
the submit path, the no-submit path, the wait_each toggle, and input
validation (exactly one of url/session_id). Driven against the
FakeBrowserPool — no real browser required.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from aakaar.capabilities import load_into
from aakaar.capabilities.open_url import CAP_REF as OPEN_REF
from aakaar.capabilities.web.web_form_fill import CAP_REF
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


def _build(tmp_path: Path, *, next_sessions: list[FakeBrowserSession] | None = None):
    tenant_id = uuid.uuid4()
    pool = FakeBrowserPool(next_sessions=next_sessions or [])
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
            CAP_REF: {"primary": {"vault_ref": "", "input_defaults": {}}},
            OPEN_REF: {"primary": {"vault_ref": "", "input_defaults": {}}},
        },
    )
    ctx = RunContext(run_id=actx.run_id, tenant_id=tenant_id, activity_ctx=actx)
    return actx, ctx, activities, pool


def _executor(activities: ActivityRegistry) -> LocalExecutor:
    return LocalExecutor(
        activities=activities, recorder=InMemoryEventRecorder(), signals=SignalHub()
    )


@pytest.mark.asyncio
async def test_fresh_session_fills_and_submits(tmp_path: Path) -> None:
    sess = FakeBrowserSession()
    actx, ctx, activities, pool = _build(tmp_path, next_sessions=[sess])
    dag = Dag(
        nodes=[
            Node(
                id="form",
                kind=NodeKind.CAPABILITY,
                ref=CAP_REF,
                inputs={
                    "url": "https://aakaar.test/contact",
                    "fields": [
                        {"selector": "#name", "value": "Ada"},
                        {"selector": "#email", "value": "ada@x.test"},
                    ],
                    "submit_selector": "#send",
                },
            )
        ]
    )
    outcome = await _executor(activities).execute(dag, ctx)
    assert outcome.status == "succeeded", outcome.error
    assert outcome.outputs["form"] == {
        "session_id": sess.id,
        "filled": 2,
        "submitted": True,
    }

    kinds = [c[0] for c in sess.calls]
    # navigate, wait+fill per field (2), then click.
    assert kinds == [
        "navigate",
        "wait_for",
        "fill",
        "wait_for",
        "fill",
        "click",
    ]
    assert sess.calls[0][1]["url"] == "https://aakaar.test/contact"
    fill_pairs = [(c[1]["selector"], c[1]["value"]) for c in sess.calls if c[0] == "fill"]
    assert fill_pairs == [("#name", "Ada"), ("#email", "ada@x.test")]
    assert sess.calls[-1] == ("click", {"selector": "#send"})
    # Fresh session must be stashed for downstream reuse.
    from aakaar.interpreter.activities.browser import _stash_key

    assert _stash_key(sess.id) in actx.session_state


@pytest.mark.asyncio
async def test_fresh_session_no_submit(tmp_path: Path) -> None:
    sess = FakeBrowserSession()
    _actx, ctx, activities, _pool = _build(tmp_path, next_sessions=[sess])
    dag = Dag(
        nodes=[
            Node(
                id="form",
                kind=NodeKind.CAPABILITY,
                ref=CAP_REF,
                inputs={
                    "url": "https://aakaar.test/x",
                    "fields": [{"selector": "#q", "value": "hello"}],
                    "wait_each": False,
                },
            )
        ]
    )
    outcome = await _executor(activities).execute(dag, ctx)
    assert outcome.status == "succeeded", outcome.error
    assert outcome.outputs["form"] == {
        "session_id": sess.id,
        "filled": 1,
        "submitted": False,
    }
    kinds = [c[0] for c in sess.calls]
    # wait_each=False -> no wait_for; no submit_selector -> no click.
    assert kinds == ["navigate", "fill"]


@pytest.mark.asyncio
async def test_reuses_upstream_session(tmp_path: Path) -> None:
    """cap.open_url opens a session; cap.web_form_fill fills the same page
    by session_id without opening a second browser."""
    sess = FakeBrowserSession()
    _actx, ctx, activities, pool = _build(tmp_path, next_sessions=[sess])
    dag = Dag(
        nodes=[
            Node(
                id="open",
                kind=NodeKind.CAPABILITY,
                ref=OPEN_REF,
                inputs={"url": "https://aakaar.test/app"},
                outputs_as="open",
            ),
            Node(
                id="form",
                kind=NodeKind.CAPABILITY,
                ref=CAP_REF,
                inputs={
                    "session_id": "${open.session}",
                    "fields": [{"selector": "#search", "value": "invoices"}],
                    "submit_selector": "#go",
                },
            ),
        ],
        edges=[Edge.model_validate({"from": "open", "to": "form"})],
    )
    outcome = await _executor(activities).execute(dag, ctx)
    assert outcome.status == "succeeded", outcome.error
    assert outcome.outputs["form"] == {
        "session_id": sess.id,
        "filled": 1,
        "submitted": True,
    }
    # Exactly one session was ever handed out (the form reused it).
    assert len(pool.handed_out) == 1
    fills = [c for c in sess.calls if c[0] == "fill"]
    assert fills == [("fill", {"selector": "#search", "value": "invoices"})]
    assert ("click", {"selector": "#go"}) in sess.calls


@pytest.mark.asyncio
async def test_rejects_both_url_and_session(tmp_path: Path) -> None:
    sess = FakeBrowserSession()
    _actx, ctx, activities, _pool = _build(tmp_path, next_sessions=[sess])
    dag = Dag(
        nodes=[
            Node(
                id="form",
                kind=NodeKind.CAPABILITY,
                ref=CAP_REF,
                inputs={
                    "url": "https://aakaar.test/x",
                    "session_id": "fake-123",
                    "fields": [{"selector": "#q", "value": "v"}],
                },
            )
        ]
    )
    outcome = await _executor(activities).execute(dag, ctx)
    assert outcome.status == "failed"


@pytest.mark.asyncio
async def test_rejects_neither_url_nor_session(tmp_path: Path) -> None:
    _actx, ctx, activities, _pool = _build(tmp_path)
    dag = Dag(
        nodes=[
            Node(
                id="form",
                kind=NodeKind.CAPABILITY,
                ref=CAP_REF,
                inputs={"fields": [{"selector": "#q", "value": "v"}]},
            )
        ]
    )
    outcome = await _executor(activities).execute(dag, ctx)
    assert outcome.status == "failed"


def test_input_schema_requires_at_least_one_field() -> None:
    from aakaar.capabilities.web.web_form_fill import _Inputs

    with pytest.raises(ValidationError):
        _Inputs(url="https://x.test", fields=[])


def test_definition_declares_no_secrets() -> None:
    from aakaar.capabilities.web.web_form_fill import definition

    assert definition.ref == "cap.web_form_fill"
    assert definition.secrets == ()
