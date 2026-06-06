"""End-to-end tests for cap.file_upload.

Stages a payload in managed storage, runs login → upload, asserts the
browser was driven through wait → upload → optional submit → optional
success-wait, and that the right bytes were materialized for the upload.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from aakaar.capabilities import load_into
from aakaar.capabilities.file_upload import CAP_REF as UPLOAD_REF
from aakaar.capabilities.web_login import CAP_REF as LOGIN_REF
from aakaar.interpreter import LocalExecutor, RunContext, build_default_activities
from aakaar.interpreter.activities.types import ActivityContext
from aakaar.interpreter.events import InMemoryEventRecorder
from aakaar.interpreter.signals import SignalHub
from aakaar.shared.dag.types import Dag, Edge, Node, NodeKind
from aakaar.shared.registry import build_default_registry
from aakaar.storage import LocalFsObjectStore
from aakaar.vault import LocalVault
from aakaar.workers.browser import FakeBrowserPool, FakeBrowserSession
from tests._discovery_helpers import discovery_response


@pytest.mark.asyncio
async def test_file_upload_full_flow(tmp_path: Path) -> None:
    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    vault_ref = f"grants/{uuid.uuid4()}"
    vault.put(str(tenant_id), vault_ref, {"username": "u", "password": "p"})

    object_store = LocalFsObjectStore(tmp_path / "objs")
    payload = b"hello,world\n1,2\n"
    obj = object_store.put(str(tenant_id), "stage/upload.csv", payload)

    sess = FakeBrowserSession(evaluate_responses=discovery_response())
    pool = FakeBrowserPool(next_sessions=[sess])

    registry = build_default_registry()
    activities = build_default_activities()
    load_into(registry, activities)

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
                id="login", kind=NodeKind.CAPABILITY, ref=LOGIN_REF,
                inputs={"account_alias": "primary", "login_url": "https://app.payops.test/login"},
                outputs_as="login",
            ),
            Node(
                id="up", kind=NodeKind.CAPABILITY, ref=UPLOAD_REF,
                inputs={
                    "session": "${login.session}",
                    "file_uri": obj.uri,
                    "file_input_selector": "input[type='file']",
                    "submit_selector": "button#upload",
                    "success_selector": "div.toast.success",
                },
            ),
        ],
        edges=[Edge.model_validate({"from": "login", "to": "up"})],
    )
    executor = LocalExecutor(
        activities=activities, recorder=InMemoryEventRecorder(), signals=SignalHub()
    )
    outcome = await executor.execute(dag, ctx)
    assert outcome.status == "succeeded", outcome.error
    assert outcome.outputs["up"] == {"file_uri": obj.uri}

    # The upload reuses the login's session, so sess.calls contains login's
    # calls first. Slice to the post-login tail and assert the upload sequence.
    upload_calls = [c for c in sess.calls if c[0] == "upload"]
    assert len(upload_calls) == 1
    assert upload_calls[0][1]["selector"] == "input[type='file']"
    local_path = Path(upload_calls[0][1]["file_path"])

    upload_idx = next(i for i, c in enumerate(sess.calls) if c[0] == "upload")
    tail = sess.calls[upload_idx - 1 : upload_idx + 3]  # wait_for, upload, click, wait_for
    kinds = [c[0] for c in tail]
    assert kinds == ["wait_for", "upload", "click", "wait_for"]
    assert tail[0][1]["selector"] == "input[type='file']"
    assert tail[2][1]["selector"] == "button#upload"
    assert tail[3][1]["selector"] == "div.toast.success"
    # Playwright receives a path whose basename is the user-facing filename
    # (so the third-party server records `upload.csv`, not `tmpXXXXXX.csv`),
    # and the staging directory lives under the system temp dir.
    import tempfile as _tempfile
    assert local_path.name == "upload.csv"
    assert local_path.parent.name.startswith("aakaar-upload-")
    assert str(local_path).startswith(_tempfile.gettempdir())
    # The staging dir is removed in the handler's `finally` after the
    # upload completes — no leftover files.
    assert not local_path.parent.exists(), (
        f"staging dir not cleaned up: {local_path.parent}"
    )


@pytest.mark.asyncio
async def test_file_upload_minimal_no_submit_no_success(tmp_path: Path) -> None:
    """Form that auto-submits on selection — only `file_input_selector` matters."""
    tenant_id = uuid.uuid4()
    vault = LocalVault(tmp_path / "vault")
    vault_ref = f"grants/{uuid.uuid4()}"
    vault.put(str(tenant_id), vault_ref, {"username": "u", "password": "p"})

    object_store = LocalFsObjectStore(tmp_path / "objs")
    obj = object_store.put(str(tenant_id), "stage/x.bin", b"x")

    sess = FakeBrowserSession(evaluate_responses=discovery_response())
    pool = FakeBrowserPool(next_sessions=[sess])

    registry = build_default_registry()
    activities = build_default_activities()
    load_into(registry, activities)
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
                id="login", kind=NodeKind.CAPABILITY, ref=LOGIN_REF,
                inputs={"account_alias": "primary", "login_url": "https://example.test/login"},
                outputs_as="login",
            ),
            Node(
                id="up", kind=NodeKind.CAPABILITY, ref=UPLOAD_REF,
                inputs={
                    "session": "${login.session}",
                    "file_uri": obj.uri,
                    "file_input_selector": "#picker",
                },
            ),
        ],
        edges=[Edge.model_validate({"from": "login", "to": "up"})],
    )
    executor = LocalExecutor(
        activities=activities, recorder=InMemoryEventRecorder(), signals=SignalHub()
    )
    outcome = await executor.execute(dag, ctx)
    assert outcome.status == "succeeded", outcome.error
    upload_idx = next(i for i, c in enumerate(sess.calls) if c[0] == "upload")
    tail = sess.calls[upload_idx - 1 : upload_idx + 1]
    assert [c[0] for c in tail] == ["wait_for", "upload"]
    assert tail[0][1]["selector"] == "#picker"
    assert tail[1][1]["selector"] == "#picker"


def test_user_facing_basename_recovers_original_filename() -> None:
    """Storage keys are `<uuid32hex>_<original>.ext` (file.read_local +
    cap.file_download both write that shape). When we stage for upload
    we want the original name back so the third party records something
    a human can recognise — `biller_transactions_2026_05.csv`, not
    `tmpXXXXXX.csv` and not `<uuid>_biller_transactions_2026_05.csv`."""
    from aakaar.capabilities.file_upload import _user_facing_basename

    uuid_prefixed = (
        "aakaar://t/6ce6045c-877e-4cd8-a222-a1375202ecc1/runs/"
        "f4f1f8b5-e09d-422f-8726-910268b1765f/downloads/"
        "72e283765f6347f6abc12345abc12345_biller_transactions_2026_05.csv"
    )
    assert _user_facing_basename(uuid_prefixed) == "biller_transactions_2026_05.csv"

    # Already-clean key (no uuid prefix) — keep as-is.
    plain = "aakaar://t/abc/stage/upload.csv"
    assert _user_facing_basename(plain) == "upload.csv"

    # A 32-hex-only filename (no underscore-separated original) should
    # be kept verbatim — we don't strip the uuid in that case because
    # there's nothing else to fall back to.
    hex_only = "aakaar://t/abc/runs/r/downloads/72e283765f6347f6abc12345abc12345.csv"
    assert _user_facing_basename(hex_only) == "72e283765f6347f6abc12345abc12345.csv"

    # Degenerate URI — defensive fallback.
    assert _user_facing_basename("") == "upload.bin"


def test_file_upload_rejects_non_managed_uri() -> None:
    from aakaar.capabilities.file_upload import _Inputs

    # Schema accepts any string at the model level (validation happens in the
    # handler) — so this only proves _Inputs accepts the literal; the runtime
    # check is exercised by the next test via the executor.
    inp = _Inputs(
        session="s",
        file_uri="file:///etc/passwd",
        file_input_selector="#x",
    )
    assert inp.file_uri == "file:///etc/passwd"


@pytest.mark.asyncio
async def test_file_upload_runtime_rejects_non_managed_uri(tmp_path: Path) -> None:
    """The handler refuses any URI that isn't an `aakaar://` managed-storage
    reference — protects against arbitrary local-file reads through the DAG."""
    from aakaar.capabilities.file_upload import handler

    sess = FakeBrowserSession(evaluate_responses=discovery_response())
    pool = FakeBrowserPool(next_sessions=[sess])
    # Pre-stash the session as if open_session had run.
    from aakaar.interpreter.activities.browser import _SessionHolder, _stash_key
    cm = pool.checkout()
    s = await cm.__aenter__()
    holder = _SessionHolder(cm=cm, session=s)
    object_store = LocalFsObjectStore(tmp_path / "objs")
    actx = ActivityContext(
        tenant_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        registry=build_default_registry(),
        object_store=object_store,
        vault=LocalVault(tmp_path / "vault"),
        browser_pool=pool,
    )
    actx.session_state[_stash_key(s.id)] = holder

    with pytest.raises(ValueError, match="aakaar://"):
        await handler(
            actx,
            {
                "session": s.id,
                "file_uri": "file:///etc/passwd",
                "file_input_selector": "#picker",
            },
        )
