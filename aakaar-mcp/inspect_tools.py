"""Dependency-free smoke-test client for the Aakaar MCP server.

Spawns `server.py` as a subprocess, talks JSON-RPC 2.0 over its stdin/stdout
(newline-delimited), and drives a minimal MCP handshake:

    initialize -> notifications/initialized -> tools/list -> tools/call

Prints what it gets back. This is a *manual* test harness, not a unit test —
run it to eyeball that the server boots, lists tools, and answers a describe-
mode tools/call.

Usage
-----
    python inspect_tools.py
    # or pick a specific tool to call:
    python inspect_tools.py cap_db_query

It runs the server with the *same* interpreter as this script, so launch it
with the interpreter that can import `aakaar`, e.g.:

    aakaar/.venv/bin/python aakaar-mcp/inspect_tools.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")


class Client:
    """A tiny line-delimited JSON-RPC client over a subprocess's stdio."""

    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self._proc = proc
        self._next_id = 0

    def _send(self, obj: dict[str, Any]) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(obj) + "\n")
        self._proc.stdin.flush()

    def _read(self) -> dict[str, Any]:
        """Read frames until we get one that is NOT a server log line.

        The server only ever writes JSON-RPC to stdout (logs go to stderr),
        so every stdout line should already be a valid frame; we still guard
        against blank lines.
        """
        assert self._proc.stdout is not None
        while True:
            line = self._proc.stdout.readline()
            if line == "":
                raise RuntimeError("server closed stdout before replying")
            line = line.strip()
            if not line:
                continue
            return json.loads(line)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        req_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        resp = self._read()
        if resp.get("id") != req_id:
            raise RuntimeError(f"id mismatch: sent {req_id}, got {resp.get('id')}")
        return resp

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})


def _print_header(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def main(argv: list[str]) -> int:
    want_tool = argv[1] if len(argv) > 1 else None

    # Inherit the environment so AAKAAR_MCP_* and the venv carry through.
    proc = subprocess.Popen(
        [sys.executable, SERVER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,  # let the server's logs stream straight through
        text=True,
        bufsize=1,
        env=os.environ.copy(),
    )

    try:
        client = Client(proc)

        _print_header("initialize")
        resp = client.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "inspect_tools", "version": "1.0.0"},
            },
        )
        print(json.dumps(resp.get("result", resp), indent=2))

        # MCP handshake: the client signals it's ready (a notification, no id).
        client.notify("notifications/initialized")

        _print_header("ping")
        print(json.dumps(client.request("ping").get("result"), indent=2))

        _print_header("tools/list")
        resp = client.request("tools/list")
        tools = resp.get("result", {}).get("tools", [])
        print(f"got {len(tools)} tool(s)")
        for t in tools[:10]:
            print(f"  - {t['name']}: {t['description'].splitlines()[0]}")
        if len(tools) > 10:
            print(f"  ... and {len(tools) - 10} more")

        if not tools:
            print("no tools returned; nothing to call")
            return 1

        # Pick a tool to call: the requested one, or the first listed.
        chosen = None
        if want_tool:
            for t in tools:
                if t["name"] == want_tool:
                    chosen = t
                    break
            if chosen is None:
                print(f"requested tool {want_tool!r} not found; using first")
        if chosen is None:
            chosen = tools[0]

        _print_header(f"tools/call (describe) -> {chosen['name']}")
        # Send empty arguments; describe mode echoes the plan regardless of
        # whether the args validate, so this stays side-effect free.
        resp = client.request(
            "tools/call", {"name": chosen["name"], "arguments": {}}
        )
        result = resp.get("result", resp)
        print(json.dumps(result, indent=2))

        _print_header("tools/call (unknown tool -> expect -32602)")
        resp = client.request(
            "tools/call", {"name": "does_not_exist", "arguments": {}}
        )
        print(json.dumps(resp.get("error", resp), indent=2))

        print("\nOK: smoke test completed.")
        return 0
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
