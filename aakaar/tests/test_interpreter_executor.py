"""LocalExecutor — happy paths, failure handling, control nodes, signals."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from aakaar.interpreter import LocalExecutor, RunContext
from aakaar.interpreter.activities.registry import ActivityRegistry
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.interpreter.events import InMemoryEventRecorder
from aakaar.interpreter.signals import SignalHub
from aakaar.shared.dag.types import Dag, Edge, Node, NodeKind
from aakaar.shared.registry import build_default_registry


def _ctx(*, registry, object_store=None, vault=None) -> RunContext:
    tenant_id = uuid.uuid4()
    run_id = uuid.uuid4()
    activity_ctx = ActivityContext(
        tenant_id=tenant_id,
        run_id=run_id,
        registry=registry,
        object_store=object_store,  # type: ignore[arg-type]
        vault=vault,  # type: ignore[arg-type]
    )
    return RunContext(run_id=run_id, tenant_id=tenant_id, activity_ctx=activity_ctx)


@pytest.mark.asyncio
async def test_linear_run_succeeds() -> None:
    registry = build_default_registry()
    activities = ActivityRegistry()
    calls: list[tuple[str, dict]] = []

    async def fake_navigate(_actx, inputs):
        calls.append(("nav", inputs))
        return {}

    activities.register("browser.navigate", fake_navigate)
    activities.register(
        "browser.open_session",
        lambda _actx, _inputs: _async_return({"session": "S"}),
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
        edges=[Edge.model_validate({"from": "open", "to": "go"})],
    )
    recorder = InMemoryEventRecorder()
    executor = LocalExecutor(activities=activities, recorder=recorder, signals=SignalHub())
    outcome = await executor.execute(dag, _ctx(registry=registry))
    assert outcome.status == "succeeded"
    assert outcome.outputs["open"] == {"session": "S"}
    assert calls == [("nav", {"session": "S", "url": "https://x"})]


@pytest.mark.asyncio
async def test_parallel_layer_runs_concurrently() -> None:
    registry = build_default_registry()
    activities = ActivityRegistry()
    started: list[str] = []
    finished: list[str] = []
    barrier = asyncio.Event()

    async def slow(name: str):
        async def handler(_actx, _inputs):
            started.append(name)
            if len(started) == 2:
                barrier.set()
            await barrier.wait()
            finished.append(name)
            return {}
        return handler

    activities.register("browser.navigate", await slow("a"))
    activities.register("browser.click", await slow("b"))

    dag = Dag(
        nodes=[
            Node(id="x", kind=NodeKind.ACTION, ref="browser.navigate", inputs={"session": "s", "url": "u"}),
            Node(id="y", kind=NodeKind.ACTION, ref="browser.click", inputs={"session": "s", "selector": "s"}),
        ]
    )
    recorder = InMemoryEventRecorder()
    executor = LocalExecutor(activities=activities, recorder=recorder, signals=SignalHub())
    outcome = await executor.execute(dag, _ctx(registry=registry))
    assert outcome.status == "succeeded"
    assert sorted(started) == ["a", "b"]
    assert sorted(finished) == ["a", "b"]


@pytest.mark.asyncio
async def test_failed_node_aborts_run() -> None:
    activities = ActivityRegistry()

    async def boom(_actx, _inputs):
        raise RuntimeError("kaboom")

    activities.register("browser.navigate", boom)
    dag = Dag(
        nodes=[
            Node(id="bad", kind=NodeKind.ACTION, ref="browser.navigate", inputs={"session": "s", "url": "u"})
        ]
    )
    recorder = InMemoryEventRecorder()
    executor = LocalExecutor(activities=activities, recorder=recorder, signals=SignalHub())
    outcome = await executor.execute(dag, _ctx(registry=build_default_registry()))
    assert outcome.status == "failed"
    assert outcome.error and "kaboom" in outcome.error["message"]


@pytest.mark.asyncio
async def test_control_wait() -> None:
    dag = Dag(
        nodes=[Node(id="w", kind=NodeKind.CONTROL, ref="control.wait", inputs={"seconds": 0.01})]
    )
    recorder = InMemoryEventRecorder()
    executor = LocalExecutor(
        activities=ActivityRegistry(),
        recorder=recorder,
        signals=SignalHub(),
    )
    outcome = await executor.execute(dag, _ctx(registry=build_default_registry()))
    assert outcome.status == "succeeded"


@pytest.mark.asyncio
async def test_human_prompt_resumes_on_signal() -> None:
    dag = Dag(
        nodes=[
            Node(
                id="ask",
                kind=NodeKind.CONTROL,
                ref="human.prompt",
                inputs={"message": "what's the otp?", "expects": "otp"},
            )
        ]
    )
    recorder = InMemoryEventRecorder()
    signals = SignalHub()
    executor = LocalExecutor(
        activities=ActivityRegistry(), recorder=recorder, signals=signals,
    )
    ctx = _ctx(registry=build_default_registry())
    task = asyncio.create_task(executor.execute(dag, ctx))

    # Wait for the prompt to register.
    for _ in range(50):
        await asyncio.sleep(0.01)
        pending = signals.list_pending(ctx.run_id)
        if pending:
            break
    assert pending, "human.prompt did not register a pending signal"

    await signals.resolve(ctx.run_id, "ask", "123456")
    outcome = await task
    assert outcome.status == "succeeded"
    assert outcome.outputs["ask"] == {"response": "123456"}


@pytest.mark.asyncio
async def test_human_prompt_times_out() -> None:
    dag = Dag(
        nodes=[
            Node(
                id="ask",
                kind=NodeKind.CONTROL,
                ref="human.prompt",
                inputs={"message": "?", "timeout_seconds": 1},
            )
        ]
    )
    recorder = InMemoryEventRecorder()
    executor = LocalExecutor(
        activities=ActivityRegistry(), recorder=recorder, signals=SignalHub(),
    )
    outcome = await executor.execute(dag, _ctx(registry=build_default_registry()))
    assert outcome.status == "failed"
    assert outcome.error and "timed out" in outcome.error["message"]


# ---------- helpers --------------------------------------------------------


async def _async_return(value):
    return value
