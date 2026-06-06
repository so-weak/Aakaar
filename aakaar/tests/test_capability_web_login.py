"""End-to-end tests for cap.web_login.

Walks the chain: capability registered → grant in vault → DAG references
the capability → executor runs against a fake browser → outputs propagate
without secrets leaking into run state.
"""

from __future__ import annotations

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
from aakaar.shared.registry import Registry, build_default_registry
from aakaar.storage import LocalFsObjectStore
from aakaar.vault import LocalVault
from aakaar.workers.browser import FakeBrowserPool, FakeBrowserSession
from tests._discovery_helpers import discovery_response


def _make_actx(
    tmp_path: Path,
    *,
    granted: dict[str, dict],
    pool: FakeBrowserPool,
) -> tuple[ActivityContext, Registry]:
    registry = build_default_registry()
    activities = build_default_activities()
    load_into(registry, activities)
    actx = ActivityContext(
        tenant_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        registry=registry,
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=LocalVault(tmp_path / "vault"),
        browser_pool=pool,
        granted_capabilities=granted,
    )
    return actx, registry


@pytest.mark.asyncio
async def test_web_login_drives_form_with_vault_creds(tmp_path: Path) -> None:
    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    grant_id = uuid.uuid4()
    vault_ref = f"grants/{grant_id}"
    vault.put(str(tenant_id), vault_ref, {"username": "ops1", "password": "s3cret"})

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
                inputs={
                    "account_alias": "primary",
                    "login_url": "https://app.payops.test/login",
                    "username_selector": "#email",
                    "password_selector": "#password",
                    "submit_selector": "#sign-in",
                    "success_selector": "nav[aria-label='Account']",
                },
            )
        ]
    )
    executor = LocalExecutor(
        activities=activities, recorder=InMemoryEventRecorder(), signals=SignalHub()
    )
    outcome = await executor.execute(dag, ctx)
    assert outcome.status == "succeeded", outcome.error

    # The session id is the only thing the DAG sees; secrets stayed out of outputs.
    assert outcome.outputs["login"] == {"session": sess.id}

    # Browser was driven exactly as configured.
    kinds = [c[0] for c in sess.calls]
    assert kinds == ["navigate", "wait_for", "fill", "fill", "click", "wait_for"]
    assert sess.calls[0][1]["url"] == "https://app.payops.test/login"
    assert sess.calls[1][1]["selector"] == "#email"
    fill_pairs = {(c[1]["selector"], c[1]["value"]) for c in sess.calls if c[0] == "fill"}
    assert fill_pairs == {("#email", "ops1"), ("#password", "s3cret")}
    assert sess.calls[4][1]["selector"] == "#sign-in"
    assert sess.calls[5][1]["selector"] == "nav[aria-label='Account']"


@pytest.mark.asyncio
async def test_web_login_falls_back_to_username_field_disappearing(tmp_path: Path) -> None:
    """If the caller doesn't supply success_selector, the handler waits on the
    username field as a stand-in (a strict selector should still be preferred,
    but this default is enough to confirm the page transitioned)."""
    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    vault_ref = f"grants/{uuid.uuid4()}"
    vault.put(str(tenant_id), vault_ref, {"username": "u", "password": "p"})

    sess = FakeBrowserSession(evaluate_responses=discovery_response())
    pool = FakeBrowserPool(next_sessions=[sess])

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
                inputs={
                    "account_alias": "primary",
                    "login_url": "https://example.test/login",
                },
            )
        ]
    )
    executor = LocalExecutor(
        activities=activities, recorder=InMemoryEventRecorder(), signals=SignalHub()
    )
    outcome = await executor.execute(dag, ctx)
    assert outcome.status == "succeeded", outcome.error
    waits = [c[1]["selector"] for c in sess.calls if c[0] == "wait_for"]
    # Auto-discovered username selector is used both before fill and as the
    # implicit success marker (the field disappears after navigation).
    assert waits == ["input[name='username']", "input[name='username']"]


@pytest.mark.asyncio
async def test_web_login_releases_session_on_failure(tmp_path: Path) -> None:
    """When the post-submit wait times out, the handler must release the
    browser checkout — leaving it open would leak a worker."""
    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    vault_ref = f"grants/{uuid.uuid4()}"
    vault.put(str(tenant_id), vault_ref, {"username": "u", "password": "p"})

    success_marker = "nav[aria-label='Account']"
    sess = FakeBrowserSession(
        wait_failures={success_marker},
        evaluate_responses=discovery_response(),
    )
    pool = FakeBrowserPool(next_sessions=[sess])

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
                inputs={
                    "account_alias": "primary",
                    "login_url": "https://example.test/login",
                    "success_selector": success_marker,
                },
            )
        ]
    )
    executor = LocalExecutor(
        activities=activities, recorder=InMemoryEventRecorder(), signals=SignalHub()
    )
    outcome = await executor.execute(dag, ctx)
    assert outcome.status == "failed"
    assert sess.closed, "checkout must be released even when login fails"
