"""Agent entrypoint.

Config comes from flags or env (``AAKAAR_AGENT_SERVER``, ``AAKAAR_AGENT_KEY``).
The server value is a base WS URL (e.g. ``wss://aakaar.lan:8000``); we append
``/ws/agents``. Deployed unattended via a per-OS service supervisor (Windows
service / launchd / systemd) — see the agent install docs.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from aakaar_agent.client import AgentClient


def _parse() -> tuple[str | None, str | None]:
    p = argparse.ArgumentParser(prog="aakaar-agent")
    p.add_argument("--server", default=os.environ.get("AAKAAR_AGENT_SERVER"))
    p.add_argument("--key", default=os.environ.get("AAKAAR_AGENT_KEY"))
    args = p.parse_args()
    return args.server, args.key


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("AAKAAR_AGENT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    server, key = _parse()
    if not server or not key:
        raise SystemExit(
            "set --server and --key (or AAKAAR_AGENT_SERVER / AAKAAR_AGENT_KEY)"
        )
    ws_url = server.rstrip("/") + "/ws/agents"
    client = AgentClient(ws_url, key)
    try:
        asyncio.run(client.run_forever())
    except KeyboardInterrupt:  # pragma: no cover
        pass


if __name__ == "__main__":  # pragma: no cover
    main()
