"""Browser primitives via FakeBrowserPool."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from aakaar.interpreter import LocalExecutor, RunContext, build_default_activities
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.interpreter.events import InMemoryEventRecorder
from aakaar.interpreter.signals import SignalHub
from aakaar.shared.dag.types import Dag, Edge, Node, NodeKind
from aakaar.shared.registry import build_default_registry
from aakaar.storage import LocalFsObjectStore
from aakaar.storage.object_store import parse_uri
from aakaar.vault import LocalVault
from aakaar.workers.browser import FakeBrowserPool, FakeBrowserSession


def _ctx(tmp_path: Path, *, pool: FakeBrowserPool) -> RunContext:
    actx = ActivityContext(
        tenant_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        registry=build_default_registry(),
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=LocalVault(tmp_path / "vault"),
        browser_pool=pool,
    )
    return RunContext(run_id=actx.run_id, tenant_id=actx.tenant_id, activity_ctx=actx)


def _executor() -> LocalExecutor:
    return LocalExecutor(
        activities=build_default_activities(),
        recorder=InMemoryEventRecorder(),
        signals=SignalHub(),
    )


def _edge(a: str, b: str) -> Edge:
    return Edge.model_validate({"from": a, "to": b})


@pytest.mark.asyncio
async def test_live_screenshots_emit_event_per_browser_node(
    tmp_path: Path,
) -> None:
    """With live_screenshots=True, the executor records a LIVE_SCREEN
    event after every node that touched a browser session, with a URI
    pointing at a stored screenshot."""
    sess = FakeBrowserSession()
    pool = FakeBrowserPool(next_sessions=[sess])
    ctx = _ctx(tmp_path, pool=pool)

    recorder = InMemoryEventRecorder()
    executor = LocalExecutor(
        activities=build_default_activities(),
        recorder=recorder,
        signals=SignalHub(),
        live_screenshots=True,
    )

    dag = Dag(
        nodes=[
            Node(id="open", kind=NodeKind.ACTION, ref="browser.open_session"),
            Node(
                id="go",
                kind=NodeKind.ACTION,
                ref="browser.navigate",
                inputs={"session": "${open.session}", "url": "https://x"},
            ),
        ],
        edges=[_edge("open", "go")],
    )
    outcome = await executor.execute(dag, ctx)
    assert outcome.status == "succeeded", outcome.error

    all_events = recorder.events.get(ctx.run_id, [])
    live_events = [e for e in all_events if e.kind == "live_screen"]
    # Both nodes touch a session, so both should have produced a live screen.
    assert len(live_events) == 2
    for ev in live_events:
        assert ev.payload["uri"].startswith("aakaar://")
        # The stored bytes are the FakeBrowserSession's PNG header.
        data = ctx.activity_ctx.object_store.get(ev.payload["uri"])
        assert data.startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_open_navigate_close(tmp_path: Path) -> None:
    sess = FakeBrowserSession()
    pool = FakeBrowserPool(next_sessions=[sess])
    ctx = _ctx(tmp_path, pool=pool)

    dag = Dag(
        nodes=[
            Node(id="open", kind=NodeKind.ACTION, ref="browser.open_session"),
            Node(
                id="go",
                kind=NodeKind.ACTION,
                ref="browser.navigate",
                inputs={"session": "${open.session}", "url": "https://x"},
            ),
            Node(
                id="close",
                kind=NodeKind.ACTION,
                ref="browser.close_session",
                inputs={"session": "${open.session}"},
            ),
        ],
        edges=[_edge("open", "go"), _edge("go", "close")],
    )
    outcome = await _executor().execute(dag, ctx)
    assert outcome.status == "succeeded", outcome.error
    methods = [c[0] for c in sess.calls]
    assert methods == ["navigate", "close"]
    assert sess.closed


@pytest.mark.asyncio
async def test_extract_passes_value_downstream(tmp_path: Path) -> None:
    sess = FakeBrowserSession(extract_responses={"#stmt-balance": "₹1,234.56"})
    pool = FakeBrowserPool(next_sessions=[sess])
    ctx = _ctx(tmp_path, pool=pool)
    activities = build_default_activities()

    captured: dict = {}

    async def capture(_actx, inputs):
        captured.update(inputs)
        return {}

    activities.register("test.capture", capture)

    dag = Dag(
        nodes=[
            Node(id="open", kind=NodeKind.ACTION, ref="browser.open_session"),
            Node(
                id="ext",
                kind=NodeKind.ACTION,
                ref="browser.extract",
                inputs={"session": "${open.session}", "selector": "#stmt-balance"},
            ),
            Node(
                id="cap",
                kind=NodeKind.ACTION,
                ref="test.capture",
                inputs={"value": "${ext.value}"},
            ),
        ],
        edges=[_edge("open", "ext"), _edge("ext", "cap")],
    )
    executor = LocalExecutor(activities=activities, recorder=InMemoryEventRecorder(), signals=SignalHub())
    outcome = await executor.execute(dag, ctx)
    assert outcome.status == "succeeded", outcome.error
    assert captured == {"value": "₹1,234.56"}


@pytest.mark.asyncio
async def test_download_lands_in_object_store(tmp_path: Path) -> None:
    sess = FakeBrowserSession(
        download_responses={"#download-pdf": ("statement.pdf", b"%PDF-1.7\nfake pdf")}
    )
    pool = FakeBrowserPool(next_sessions=[sess])
    ctx = _ctx(tmp_path, pool=pool)

    dag = Dag(
        nodes=[
            Node(id="open", kind=NodeKind.ACTION, ref="browser.open_session"),
            Node(
                id="dl",
                kind=NodeKind.ACTION,
                ref="browser.download",
                inputs={
                    "session": "${open.session}",
                    "trigger_selector": "#download-pdf",
                },
            ),
        ],
        edges=[_edge("open", "dl")],
    )
    outcome = await _executor().execute(dag, ctx)
    assert outcome.status == "succeeded", outcome.error
    uri = outcome.outputs["dl"]["file_uri"]
    tenant_id, key = parse_uri(uri)
    assert tenant_id == str(ctx.tenant_id)
    assert key.endswith("statement.pdf")
    data = ctx.activity_ctx.object_store.get(uri)
    assert data.startswith(b"%PDF-")


@pytest.mark.asyncio
async def test_session_cleaned_up_on_run_end(tmp_path: Path) -> None:
    """If a workflow forgets to close_session, the orchestrator's hook closes it."""
    sess = FakeBrowserSession()
    pool = FakeBrowserPool(next_sessions=[sess])
    ctx = _ctx(tmp_path, pool=pool)

    dag = Dag(
        nodes=[
            Node(id="open", kind=NodeKind.ACTION, ref="browser.open_session"),
            Node(
                id="nav",
                kind=NodeKind.ACTION,
                ref="browser.navigate",
                inputs={"session": "${open.session}", "url": "https://x"},
            ),
        ],
        edges=[_edge("open", "nav")],
    )
    outcome = await _executor().execute(dag, ctx)
    assert outcome.status == "succeeded"
    # Executor doesn't close — orchestrator does. Simulate it manually here.
    for value in list(ctx.activity_ctx.session_state.values()):
        await value.close()
    assert sess.closed


@pytest.mark.asyncio
async def test_browser_pool_required(tmp_path: Path) -> None:
    """Without a pool, browser.open_session raises with a clear message."""
    actx = ActivityContext(
        tenant_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        registry=build_default_registry(),
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=LocalVault(tmp_path / "vault"),
        browser_pool=None,
    )
    ctx = RunContext(run_id=actx.run_id, tenant_id=actx.tenant_id, activity_ctx=actx)
    dag = Dag(nodes=[Node(id="open", kind=NodeKind.ACTION, ref="browser.open_session")])
    outcome = await _executor().execute(dag, ctx)
    assert outcome.status == "failed"
    assert "browser_pool" in (outcome.error or {}).get("message", "")
