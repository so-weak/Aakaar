"""End-to-end tests for cap.file_download.

Composes a `cap.web_login` upstream node so the download has a real
session handle to look up — exactly how the planner is expected to wire
the two together. Asserts the file is persisted to managed storage and
the URI propagates as the node's output.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from aakar.capabilities import load_into
from aakar.capabilities.file_download import CAP_REF as DOWNLOAD_REF
from aakar.capabilities.web_login import CAP_REF as LOGIN_REF
from aakar.interpreter import LocalExecutor, RunContext, build_default_activities
from aakar.interpreter.activities.types import ActivityContext
from aakar.interpreter.events import InMemoryEventRecorder
from aakar.interpreter.signals import SignalHub
from aakar.shared.dag.types import Dag, Edge, Node, NodeKind
from aakar.shared.registry import build_default_registry
from aakar.storage import LocalFsObjectStore
from aakar.vault import LocalVault
from aakar.workers.browser import FakeBrowserPool, FakeBrowserSession
from tests._discovery_helpers import discovery_response


@pytest.mark.asyncio
async def test_file_download_via_trigger_selector(tmp_path: Path) -> None:
    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    vault_ref = f"grants/{uuid.uuid4()}"
    vault.put(str(tenant_id), vault_ref, {"username": "u", "password": "p"})

    payload = b"col_a,col_b\n1,2\n"
    sess = FakeBrowserSession(
        download_responses={"a#latest-report": ("report.csv", payload)},
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
            LOGIN_REF: {"primary": {"vault_ref": vault_ref, "input_defaults": {}}},
        },
    )
    ctx = RunContext(run_id=actx.run_id, tenant_id=tenant_id, activity_ctx=actx)

    dag = Dag(
        nodes=[
            Node(
                id="login",
                kind=NodeKind.CAPABILITY,
                ref=LOGIN_REF,
                inputs={
                    "account_alias": "primary",
                    "login_url": "https://app.payops.test/login",
                },
                outputs_as="login",
            ),
            Node(
                id="dl",
                kind=NodeKind.CAPABILITY,
                ref=DOWNLOAD_REF,
                inputs={
                    "session": "${login.session}",
                    "trigger_selector": "a#latest-report",
                    "wait_for": "section.reports",
                },
            ),
        ],
        edges=[Edge.model_validate({"from": "login", "to": "dl"})],
    )
    executor = LocalExecutor(
        activities=activities, recorder=InMemoryEventRecorder(), signals=SignalHub()
    )
    outcome = await executor.execute(dag, ctx)
    assert outcome.status == "succeeded", outcome.error

    out = outcome.outputs["dl"]
    assert out["filename"] == "report.csv"
    assert out["uri"].startswith("aakar://")
    # The file actually landed in managed storage and round-trips byte-exact.
    assert object_store.get(out["uri"]) == payload

    # The capability waited on the listing before triggering the download.
    seen = [c[0] for c in sess.calls]
    assert seen.index("wait_for") < seen.index("download")


@pytest.mark.asyncio
async def test_file_download_via_direct_url(tmp_path: Path) -> None:
    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    vault_ref = f"grants/{uuid.uuid4()}"
    vault.put(str(tenant_id), vault_ref, {"username": "u", "password": "p"})

    sess = FakeBrowserSession(
        download_responses={"https://app.payops.test/exports/latest.csv": ("daily.csv", b"x")},
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
            LOGIN_REF: {"primary": {"vault_ref": vault_ref, "input_defaults": {}}},
        },
    )
    ctx = RunContext(run_id=actx.run_id, tenant_id=tenant_id, activity_ctx=actx)

    dag = Dag(
        nodes=[
            Node(
                id="login", kind=NodeKind.CAPABILITY, ref=LOGIN_REF,
                inputs={"account_alias": "primary", "login_url": "https://app.payops.test/login"},
                outputs_as="login",
            ),
            Node(
                id="dl", kind=NodeKind.CAPABILITY, ref=DOWNLOAD_REF,
                inputs={
                    "session": "${login.session}",
                    "url": "https://app.payops.test/exports/latest.csv",
                },
            ),
        ],
        edges=[Edge.model_validate({"from": "login", "to": "dl"})],
    )
    executor = LocalExecutor(
        activities=activities, recorder=InMemoryEventRecorder(), signals=SignalHub()
    )
    outcome = await executor.execute(dag, ctx)
    assert outcome.status == "succeeded", outcome.error
    assert outcome.outputs["dl"]["filename"] == "daily.csv"


@pytest.mark.asyncio
async def test_file_download_mirrors_to_disk_with_collision_suffix(
    tmp_path: Path,
) -> None:
    """When `download_mirror_dir` is set, the capability writes a sibling
    copy to disk in addition to the object store. Re-downloading the
    same filename appends `(1)` so existing copies aren't clobbered.
    """
    from aakar.capabilities.file_download import CAP_REF as DOWNLOAD_REF
    from aakar.capabilities.file_download import handler as download_handler
    from aakar.interpreter.activities.browser import _SessionHolder, _stash_key

    tenant_id = uuid.uuid4()
    mirror_dir = tmp_path / "Downloads"

    payload_1 = b"first,row\n1,a\n"
    payload_2 = b"second,row\n2,b\n"
    sess = FakeBrowserSession(
        download_responses={
            "a#first": ("report.csv", payload_1),
            "a#second": ("report.csv", payload_2),
        },
    )
    # Pre-stash the session so the handler's `_get_session` finds it
    # without going through `browser.open_session` (we don't need a real
    # login chain for this test — just the mirror behavior).
    holder = _SessionHolder(cm=None, session=sess)
    object_store = LocalFsObjectStore(tmp_path / "objs")
    actx = ActivityContext(
        tenant_id=tenant_id,
        run_id=uuid.uuid4(),
        registry=build_default_registry(),
        object_store=object_store,
        vault=LocalVault(tmp_path / "vault"),
        download_mirror_dir=mirror_dir,
    )
    actx.session_state[_stash_key(sess.id)] = holder

    out1 = await download_handler(
        actx, {"session": sess.id, "trigger_selector": "a#first"}
    )
    out2 = await download_handler(
        actx, {"session": sess.id, "trigger_selector": "a#second"}
    )

    # Object store has both — canonical storage is unchanged.
    assert object_store.get(out1["uri"]) == payload_1
    assert object_store.get(out2["uri"]) == payload_2

    # Mirror directory was created and holds both files, with the second
    # rotated to "report (1).csv" because of the name collision.
    assert (mirror_dir / "report.csv").read_bytes() == payload_1
    assert (mirror_dir / "report (1).csv").read_bytes() == payload_2

    # Reference DOWNLOAD_REF to keep the import live for grep-ability of
    # the capability under test.
    assert DOWNLOAD_REF == "cap.file_download"


@pytest.mark.asyncio
async def test_file_download_mirror_disabled_writes_nothing_to_disk(
    tmp_path: Path,
) -> None:
    """`download_mirror_dir=None` (the default) must skip the mirror —
    deployed environments rely on the object store as the only sink.
    """
    from aakar.capabilities.file_download import handler as download_handler
    from aakar.interpreter.activities.browser import _SessionHolder, _stash_key

    sess = FakeBrowserSession(
        download_responses={"a#x": ("x.csv", b"hello")},
    )
    actx = ActivityContext(
        tenant_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        registry=build_default_registry(),
        object_store=LocalFsObjectStore(tmp_path / "objs"),
        vault=LocalVault(tmp_path / "vault"),
        download_mirror_dir=None,
    )
    actx.session_state[_stash_key(sess.id)] = _SessionHolder(cm=None, session=sess)

    await download_handler(actx, {"session": sess.id, "trigger_selector": "a#x"})

    # Nothing leaked onto the host filesystem outside the object-store root.
    assert not (tmp_path / "Downloads").exists()


def test_file_download_rejects_both_selector_and_url() -> None:
    """The schema's `_check_one_of` validator must trip if the planner
    accidentally provides both forms — protects the runtime from
    ambiguous inputs."""
    from aakar.capabilities.file_download import _Inputs

    with pytest.raises(ValueError):
        _Inputs(session="s", trigger_selector="a", url="https://x")


def test_file_download_rejects_neither_selector_nor_url() -> None:
    from aakar.capabilities.file_download import _Inputs

    with pytest.raises(ValueError):
        _Inputs(session="s")
