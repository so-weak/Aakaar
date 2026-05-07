"""browser.fill_secret action — vault-aware fill primitive.

Lets the planner compose multi-step login flows (captcha mid-form, MFA,
etc.) without putting a credential value into the DAG JSON. The fill is
keyed by (capability_ref, account_alias, secret_name).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from aakar.interpreter import LocalExecutor, RunContext, build_default_activities
from aakar.interpreter.activities.browser import _SessionHolder, _stash_key
from aakar.interpreter.activities.types import ActivityContext
from aakar.interpreter.events import InMemoryEventRecorder
from aakar.interpreter.signals import SignalHub
from aakar.shared.dag.types import Dag, Edge, Node, NodeKind
from aakar.shared.registry import build_default_registry
from aakar.storage import LocalFsObjectStore
from aakar.vault import LocalVault
from aakar.workers.browser import FakeBrowserPool, FakeBrowserSession


@pytest.mark.asyncio
async def test_fill_secret_writes_value_without_leaking_into_outputs(tmp_path: Path) -> None:
    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    vault_ref = f"grants/{uuid.uuid4()}"
    vault.put(str(tenant_id), vault_ref, {"username": "ops1", "password": "s3cret"})

    sess = FakeBrowserSession()
    pool = FakeBrowserPool(next_sessions=[sess])

    activities = build_default_activities()
    actx = ActivityContext(
        tenant_id=tenant_id,
        run_id=uuid.uuid4(),
        registry=build_default_registry(),
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=vault,
        browser_pool=pool,
        granted_capabilities={
            "cap.web_login": {"primary": {"vault_ref": vault_ref, "input_defaults": {}}},
        },
    )
    ctx = RunContext(run_id=actx.run_id, tenant_id=tenant_id, activity_ctx=actx)

    dag = Dag(
        nodes=[
            Node(id="open", kind=NodeKind.ACTION, ref="browser.open_session", inputs={}),
            Node(
                id="fill_pw",
                kind=NodeKind.ACTION,
                ref="browser.fill_secret",
                inputs={
                    "session": "${open.session}",
                    "selector": "#password",
                    "capability_ref": "cap.web_login",
                    "account_alias": "primary",
                    "secret_name": "password",
                },
            ),
        ],
        edges=[Edge.model_validate({"from": "open", "to": "fill_pw"})],
    )
    executor = LocalExecutor(
        activities=activities, recorder=InMemoryEventRecorder(), signals=SignalHub()
    )
    outcome = await executor.execute(dag, ctx)
    assert outcome.status == "succeeded", outcome.error

    # The actual secret value reached the browser…
    fill_calls = [c for c in sess.calls if c[0] == "fill"]
    assert fill_calls == [
        ("fill", {"selector": "#password", "value": "s3cret"}),
    ]
    # …but the node's outputs are empty (the value never flows through env).
    assert outcome.outputs["fill_pw"] == {}


@pytest.mark.asyncio
async def test_fill_secret_refuses_unknown_secret_name(tmp_path: Path) -> None:
    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    vault_ref = f"grants/{uuid.uuid4()}"
    vault.put(str(tenant_id), vault_ref, {"username": "u", "password": "p"})

    sess = FakeBrowserSession()
    pool = FakeBrowserPool(next_sessions=[sess])

    # Pre-stash a session as if open_session had run.
    activities = build_default_activities()
    actx = ActivityContext(
        tenant_id=tenant_id,
        run_id=uuid.uuid4(),
        registry=build_default_registry(),
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=vault,
        browser_pool=pool,
        granted_capabilities={
            "cap.web_login": {"primary": {"vault_ref": vault_ref, "input_defaults": {}}},
        },
    )
    cm = pool.checkout()
    s = await cm.__aenter__()
    actx.session_state[_stash_key(s.id)] = _SessionHolder(cm=cm, session=s)

    from aakar.interpreter.activities.browser import fill_secret

    with pytest.raises(PermissionError, match="no secret named 'mfa_token'"):
        await fill_secret(
            actx,
            {
                "session": s.id,
                "selector": "#x",
                "capability_ref": "cap.web_login",
                "account_alias": "primary",
                "secret_name": "mfa_token",
            },
        )


@pytest.mark.asyncio
async def test_fill_secret_refuses_ungranted_capability(tmp_path: Path) -> None:
    activities = build_default_activities()
    sess = FakeBrowserSession()
    pool = FakeBrowserPool(next_sessions=[sess])
    actx = ActivityContext(
        tenant_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        registry=build_default_registry(),
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=LocalVault(tmp_path / "vault"),
        browser_pool=pool,
        granted_capabilities={},  # nothing granted
    )
    cm = pool.checkout()
    s = await cm.__aenter__()
    actx.session_state[_stash_key(s.id)] = _SessionHolder(cm=cm, session=s)

    from aakar.interpreter.activities.browser import fill_secret

    with pytest.raises(PermissionError, match="no grant"):
        await fill_secret(
            actx,
            {
                "session": s.id,
                "selector": "#x",
                "capability_ref": "cap.web_login",
                "account_alias": "primary",
                "secret_name": "password",
            },
        )
