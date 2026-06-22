"""Stage 8 — the remote_browser_enabled gate.

Browser/credential caps must not be placeable on an agent until the deployment
explicitly opts in, independent of remote_exec_enabled.
"""

from __future__ import annotations

import uuid

import pytest

from aakaar.shared.dag.types import Dag, Node, NodeKind
from aakaar.workers.remote.dispatcher import RemoteDispatcher, RemoteExecError
from aakaar.workers.remote.placement import check_placement
from aakaar.workers.remote.protocol import AgentCapability, AgentInfo
from aakaar.workers.remote.registry import AgentRegistry


class _Conn:
    def __init__(self, alias: str, tenant: uuid.UUID, refs: list[str]) -> None:
        self.info = AgentInfo(
            alias=alias, tenant_id=tenant, capabilities=tuple(AgentCapability(ref=r) for r in refs)
        )

    async def close(self) -> None:  # pragma: no cover
        pass


def _dag() -> Dag:
    return Dag(nodes=[Node(id="login", kind=NodeKind.CAPABILITY, ref="cap.web_login", target="a-mac")])


def test_placement_blocks_browser_when_flag_off() -> None:
    tenant = uuid.uuid4()
    reg = AgentRegistry()
    reg.register(_Conn("a-mac", tenant, ["cap.web_login"]))
    issues = check_placement(_dag(), tenant, agents=reg, browser_enabled=False)
    assert issues and "disabled" in issues[0]["reason"]


def test_placement_allows_browser_when_flag_on() -> None:
    tenant = uuid.uuid4()
    reg = AgentRegistry()
    reg.register(_Conn("a-mac", tenant, ["cap.web_login"]))
    issues = check_placement(_dag(), tenant, agents=reg, browser_enabled=True)
    assert issues == []


async def test_dispatcher_refuses_browser_when_flag_off() -> None:
    from aakaar.interpreter.activities.types import ActivityContext
    from aakaar.shared.registry import build_default_registry
    from aakaar.storage import LocalFsObjectStore
    import tempfile

    tenant = uuid.uuid4()
    reg = AgentRegistry()
    reg.register(_Conn("a-mac", tenant, ["cap.web_login"]))
    disp = RemoteDispatcher(agents=reg, browser_enabled=False)
    with tempfile.TemporaryDirectory() as d:
        actx = ActivityContext(
            tenant_id=tenant, run_id=uuid.uuid4(), registry=build_default_registry(),
            object_store=LocalFsObjectStore(d), vault=None,
        )
        node = Node(id="login", kind=NodeKind.CAPABILITY, ref="cap.web_login", target="a-mac")
        with pytest.raises(RemoteExecError, match="remote browser execution is disabled"):
            await disp.run(node, {"account_alias": "primary"}, actx, target="a-mac")
