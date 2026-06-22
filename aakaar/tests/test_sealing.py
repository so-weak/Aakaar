"""Stage 5 — sealed-box transport.

Credentials (server -> agent) and object bodies (both directions) are sealed to
the recipient's public key, so the broker — which relays frame bodies verbatim —
only ever sees ciphertext. These tests exercise both directions end to end with
two real keypairs.
"""

from __future__ import annotations

import base64
import json
import uuid

from aakaar_caps.sealing import Sealer, available, is_sealed
from aakaar.storage.object_store import LocalFsObjectStore
from aakaar.workers.remote.backchannel import ServerBackchannelHandler
from aakaar.workers.remote.connection import WebSocketAgentConnection
from aakaar.workers.remote.dispatcher import RemoteDispatcher
from aakaar.workers.remote.protocol import AgentInfo
from aakaar.workers.remote.registry import AgentRegistry


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, frame: dict) -> None:
        self.sent.append(frame)

    async def close(self) -> None:  # pragma: no cover
        pass


def test_pynacl_available_and_roundtrip() -> None:
    assert available()
    server, agent = Sealer.generate(), Sealer.generate()
    env = server.seal(b"hunter2", agent.public_key_hex())
    assert is_sealed(env)
    assert agent.unseal(env) == b"hunter2"


def test_dispatcher_seals_secrets_to_agent_key() -> None:
    server, agent = Sealer.generate(), Sealer.generate()
    disp = RemoteDispatcher(agents=AgentRegistry(), sealer=server)

    class _Conn:
        info = AgentInfo(alias="a", tenant_id=uuid.uuid4(), public_key=agent.public_key_hex())

    env = disp._seal_secrets({"username": "u", "password": "p"}, _Conn())
    assert is_sealed(env)
    # Only the agent's private key recovers the credentials.
    assert json.loads(agent.unseal(env)) == {"username": "u", "password": "p"}


def test_dispatcher_falls_back_to_cleartext_without_agent_key() -> None:
    server = Sealer.generate()
    disp = RemoteDispatcher(agents=AgentRegistry(), sealer=server)

    class _Conn:
        info = AgentInfo(alias="a", tenant_id=uuid.uuid4(), public_key=None)

    assert disp._seal_secrets({"x": "y"}, _Conn()) is None


async def test_obj_put_accepts_sealed_body(tmp_path) -> None:
    server, agent = Sealer.generate(), Sealer.generate()
    store = LocalFsObjectStore(tmp_path)
    tenant = uuid.uuid4()
    handler = ServerBackchannelHandler(object_store=store, sealer=server)
    conn = WebSocketAgentConnection(
        _FakeWS(), AgentInfo(alias="a", tenant_id=tenant, public_key=agent.public_key_hex())
    )
    # Agent seals the body to the server's public key.
    sealed = agent.seal(b"statement-bytes", server.public_key_hex())
    await handler(conn, {"type": "req", "request_id": "1", "op": "obj_put", "key": "runs/r/f.bin", "sealed": sealed})
    reply = conn._ws.sent[-1]  # type: ignore[attr-defined]
    assert reply["ok"]
    assert store.get(reply["result"]["uri"]) == b"statement-bytes"


async def test_obj_get_seals_reply_to_agent(tmp_path) -> None:
    server, agent = Sealer.generate(), Sealer.generate()
    store = LocalFsObjectStore(tmp_path)
    tenant = uuid.uuid4()
    obj = store.put(str(tenant), "runs/r/shot.png", b"png-bytes")
    handler = ServerBackchannelHandler(object_store=store, sealer=server)
    conn = WebSocketAgentConnection(
        _FakeWS(), AgentInfo(alias="a", tenant_id=tenant, public_key=agent.public_key_hex())
    )
    await handler(conn, {"type": "req", "request_id": "1", "op": "obj_get", "uri": obj.uri})
    reply = conn._ws.sent[-1]  # type: ignore[attr-defined]
    assert reply["ok"] and "sealed" in reply["result"] and "b64" not in reply["result"]
    assert agent.unseal(reply["result"]["sealed"]) == b"png-bytes"
