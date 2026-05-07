"""End-to-end test of the example_login capability.

Walks the full chain: capability authoring → grant credential storage →
DAG referencing the capability → executor running it against a fake
browser → outputs propagated.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from aakar.capabilities import load_into
from aakar.capabilities.example_login import CAP_REF
from aakar.interpreter import LocalExecutor, RunContext, build_default_activities
from aakar.interpreter.activities.types import ActivityContext
from aakar.interpreter.events import InMemoryEventRecorder
from aakar.interpreter.signals import SignalHub
from aakar.shared.dag.types import Dag, Node, NodeKind
from aakar.shared.registry import Registry, build_default_registry
from aakar.storage import LocalFsObjectStore
from aakar.vault import LocalVault
from aakar.workers.browser import FakeBrowserPool, FakeBrowserSession


@pytest.mark.asyncio
async def test_example_login_runs(tmp_path: Path) -> None:
    # Set up registry with the example capability loaded.
    registry: Registry = build_default_registry()
    activities = build_default_activities()
    n = load_into(registry, activities)
    assert n >= 1
    assert registry.get(CAP_REF) is not None

    # Vault: store credentials for the capability under a known vault_ref.
    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    grant_id = uuid.uuid4()
    vault_ref = f"grants/{grant_id}"
    vault.put(str(tenant_id), vault_ref, {"username": "alice", "password": "wonderland"})

    # Browser: a fake that won't fail wait_for and accepts all calls.
    sess = FakeBrowserSession()
    pool = FakeBrowserPool(next_sessions=[sess])

    actx = ActivityContext(
        tenant_id=tenant_id,
        run_id=uuid.uuid4(),
        registry=registry,
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=vault,
        browser_pool=pool,
        granted_capabilities={
            CAP_REF: {"primary": {"vault_ref": vault_ref, "input_defaults": {}}},
        },
    )
    ctx = RunContext(run_id=actx.run_id, tenant_id=tenant_id, activity_ctx=actx)

    dag = Dag(
        nodes=[
            Node(
                id="login",
                kind=NodeKind.CAPABILITY,
                ref=CAP_REF,
                inputs={"account_alias": "primary"},
            )
        ]
    )
    executor = LocalExecutor(
        activities=activities, recorder=InMemoryEventRecorder(), signals=SignalHub()
    )
    outcome = await executor.execute(dag, ctx)
    assert outcome.status == "succeeded", outcome.error

    # Credentials reached the browser via fill(), but values must not appear
    # in the run outputs.
    assert outcome.outputs["login"] == {"session": sess.id}
    fill_calls = [c for c in sess.calls if c[0] == "fill"]
    fill_values = {c[1]["value"] for c in fill_calls}
    assert fill_values == {"alice", "wonderland"}


@pytest.mark.asyncio
async def test_example_login_rejects_ungranted_alias(tmp_path: Path) -> None:
    registry = build_default_registry()
    activities = build_default_activities()
    load_into(registry, activities)

    pool = FakeBrowserPool(next_sessions=[FakeBrowserSession()])
    tenant_id = uuid.uuid4()
    actx = ActivityContext(
        tenant_id=tenant_id,
        run_id=uuid.uuid4(),
        registry=registry,
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=LocalVault(tmp_path / "vault"),
        browser_pool=pool,
        # No grant for this capability.
        granted_capabilities={},
    )
    ctx = RunContext(run_id=actx.run_id, tenant_id=tenant_id, activity_ctx=actx)
    dag = Dag(
        nodes=[
            Node(
                id="login",
                kind=NodeKind.CAPABILITY,
                ref=CAP_REF,
                inputs={"account_alias": "primary"},
            )
        ]
    )
    executor = LocalExecutor(
        activities=activities, recorder=InMemoryEventRecorder(), signals=SignalHub()
    )
    outcome = await executor.execute(dag, ctx)
    assert outcome.status == "failed"
    assert "no grant" in (outcome.error or {}).get("message", "")
