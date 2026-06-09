"""Aakaar capabilities-as-MCP stdio server.

This is a Model Context Protocol (MCP) server that projects the in-repo
Aakaar capability registry as MCP tools so any MCP-aware agent (Claude
Desktop, etc.) can discover and (optionally) invoke Aakaar capabilities.

Design at a glance
-------------------
* Transport: JSON-RPC 2.0 over stdio, newline-delimited (one JSON object
  per line). No third-party MCP SDK — only the Python standard library.
* Source of truth: the live Aakaar registry. We build it exactly the way
  the app does (`build_default_registry()` + `load_into(...)`), then turn
  each `Definition` into one MCP tool.
* Two execution modes (env `AAKAAR_MCP_MODE`):
    - "describe" (default): `tools/call` returns a side-effect-free JSON
      "plan" describing what *would* run. Nothing touches the backend.
    - "live": `tools/call` reaches the real Aakaar HTTP API, creates a
      one-node workflow and starts a run, returning the ids + status.
* stdout quarantine (see `_log` / the redirect in `_load_tools`): stdout is
  reserved *exclusively* for JSON-RPC frames. A single stray `print()` from
  a transitively-imported dependency would corrupt the framing and break the
  client, so all logging goes to stderr and stdout is redirected to stderr
  during the (import-heavy) registry build.

Environment variables
----------------------
  AAKAAR_MCP_INCLUDE   "capabilities" (default) -> only kind=="capability"
                       "all"                     -> also actions + controls
  AAKAAR_MCP_MODE      "describe" (default) | "live"
  AAKAAR_API           Base URL of the Aakaar HTTP API (live mode only)
  AAKAAR_TOKEN         Bearer token for the Aakaar API (live mode only)

Python 3.11+.  Run with: `python server.py`  (or `aakaar-mcp` console script).
"""

from __future__ import annotations

import json
import os
import sys
import traceback
import urllib.error
import urllib.request
from typing import Any

# MCP protocol revision we speak. Echoed back on initialize so a client can
# negotiate. 2024-11-05 is the revision the reference tooling targets.
PROTOCOL_VERSION = "2024-11-05"

SERVER_NAME = "aakaar"
SERVER_VERSION = "1.0.0"

# Populated lazily by _load_tools() the first time tools are needed.
#   _TOOLS:   list of MCP tool descriptors (the tools/list payload)
#   _BY_NAME: munged-tool-name -> registry Definition (so tools/call can
#             recover the real dotted ref)
_TOOLS: list[dict[str, Any]] | None = None
_BY_NAME: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# stdout quarantine + framing
# ---------------------------------------------------------------------------


def _log(*parts: object) -> None:
    """Write a diagnostic line to STDERR only.

    stdout is sacred — it carries JSON-RPC frames and nothing else. Anything
    we want a human/operator to see goes here.
    """
    try:
        msg = " ".join(str(p) for p in parts)
        sys.stderr.write(f"[aakaar-mcp] {msg}\n")
        sys.stderr.flush()
    except Exception:
        # Logging must never raise into the protocol loop.
        pass


def _write_frame(obj: dict[str, Any]) -> None:
    """Serialize one JSON object and write it to stdout as a single line."""
    data = json.dumps(obj, default=str, ensure_ascii=False)
    sys.stdout.write(data + "\n")
    sys.stdout.flush()


def _reply(req_id: Any, *, result: Any = None, error: dict[str, Any] | None = None) -> None:
    """The single choke point for every protocol response.

    Exactly one of `result` / `error` should be set. We always emit a valid
    JSON-RPC 2.0 envelope and always flush so the client sees the frame
    immediately.
    """
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    _write_frame(msg)


def _rpc_error(code: int, message: str) -> dict[str, Any]:
    return {"code": code, "message": message}


# ---------------------------------------------------------------------------
# Registry -> MCP tools
# ---------------------------------------------------------------------------


def _munge_name(ref: str) -> str:
    """MCP tool names cannot contain dots; the registry refs are dotted
    (e.g. "cap.db_query"). Map "." -> "_" so "cap.db_query" -> "cap_db_query".
    Reversibility is handled by the `_BY_NAME` lookup table, not by un-munging.
    """
    return ref.replace(".", "_")


def _build_tool(defn: Any) -> dict[str, Any]:
    """Turn one registry Definition into an MCP tool descriptor.

    Raises on a bad schema; the caller wraps this in try/except so one
    malformed definition can't sink the whole server.
    """
    # JSON Schema for inputs. `model_json_schema()` already emits an object
    # schema for these pydantic models, but we defensively force a type so a
    # stricter MCP client never trips over a missing "type".
    input_schema = defn.input_schema.model_json_schema()
    input_schema.setdefault("type", "object")

    # Output schema is an improvement over the reference projection: agents
    # that support it can validate / shape-check what they'll get back.
    output_schema = defn.output_schema.model_json_schema()
    output_schema.setdefault("type", "object")

    description = f"[{defn.ref}] {defn.description}"

    # Capabilities own credentials. Surface the *names* of the secrets a grant
    # must supply so an agent knows a tenant grant is a prerequisite. Names
    # only — never values.
    secrets = getattr(defn, "secrets", ()) or ()
    if secrets:
        names = ", ".join(s.name for s in secrets)
        description += (
            f"\nRequires a capability grant supplying secret(s): {names}."
        )

    return {
        "name": _munge_name(defn.ref),
        "description": description,
        "inputSchema": input_schema,
        "outputSchema": output_schema,
    }


def _load_tools() -> list[dict[str, Any]]:
    """Build the registry and project it into MCP tools (memoized).

    Importing `aakaar` pulls in a large dependency graph; any of those modules
    could `print()` at import time. To keep stdout clean we temporarily point
    sys.stdout at sys.stderr for the duration of the build and restore it in a
    `finally`.
    """
    global _TOOLS, _BY_NAME
    if _TOOLS is not None:
        return _TOOLS

    include = os.environ.get("AAKAAR_MCP_INCLUDE", "capabilities").strip().lower()

    saved_stdout = sys.stdout
    sys.stdout = sys.stderr  # quarantine: stray prints during import -> stderr
    try:
        # Imported lazily so a `--help`/compile-check path never needs the
        # heavy aakaar dependency tree.
        from aakaar.capabilities import load_into
        from aakaar.interpreter.activities.registry import ActivityRegistry
        from aakaar.shared.registry import build_default_registry

        reg = build_default_registry()
        load_into(reg, ActivityRegistry())
    finally:
        sys.stdout = saved_stdout

    # Decide which kinds to expose. "capabilities" (default) keeps the surface
    # tight; "all" also exposes raw actions + control nodes.
    if include == "all":
        wanted = {"capability", "action", "control"}
    else:
        wanted = {"capability"}

    tools: list[dict[str, Any]] = []
    by_name: dict[str, Any] = {}

    for defn in reg:
        kind_value = getattr(defn.kind, "value", str(defn.kind))
        if kind_value not in wanted:
            continue
        try:
            tool = _build_tool(defn)
        except Exception as exc:  # noqa: BLE001 — log+continue, never crash
            _log(f"skipping {getattr(defn, 'ref', '?')}: schema build failed: {exc}")
            continue

        name = tool["name"]
        # Name-collision guard: two distinct refs could munge to the same MCP
        # name (e.g. if a ref ever used both "." and "_"). Don't silently
        # overwrite — warn and disambiguate with a numeric suffix.
        if name in by_name:
            base = name
            suffix = 2
            while name in by_name:
                name = f"{base}_{suffix}"
                suffix += 1
            _log(
                f"NAME COLLISION: {defn.ref!r} munges to {base!r} which is taken; "
                f"using {name!r} instead"
            )
            tool["name"] = name

        tools.append(tool)
        by_name[name] = defn

    tools.sort(key=lambda t: t["name"])
    _TOOLS = tools
    _BY_NAME = by_name
    _log(f"loaded {len(tools)} tool(s) (include={include!r})")
    return _TOOLS


# ---------------------------------------------------------------------------
# tools/call — describe vs live
# ---------------------------------------------------------------------------


def _infer_kind(ref: str) -> str:
    """Infer the DAG NodeKind value from a ref prefix.

    Mirrors the backend's ref conventions:
      cap.*                          -> capability
      control.* / human.prompt       -> control
      everything else (e.g. http.*)  -> action
    """
    if ref.startswith("cap."):
        return "capability"
    if ref.startswith("control.") or ref == "human.prompt":
        return "control"
    return "action"


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    """Build the MCP CallToolResult shape."""
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _describe(ref: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Return a side-effect-free plan describing what a live call would do."""
    kind = _infer_kind(ref)
    plan = {
        "channel": "aakaar-mcp",
        "capability_ref": ref,
        "arguments": arguments,
        "execution": {
            "mode": "describe",
            "node_kind": kind,
            "would_post": [
                "POST {AAKAAR_API}/workflows",
                "POST {AAKAAR_API}/workflows/{id}/runs",
            ],
        },
        "note": (
            "Describe mode: no side effects. Set AAKAAR_MCP_MODE=live (with "
            "AAKAAR_API and AAKAAR_TOKEN) to actually create a workflow and "
            "start a run."
        ),
    }
    return _text_result(json.dumps(plan, indent=2, default=str))


def _http_json(
    url: str, body: dict[str, Any], token: str, timeout: float = 30.0
) -> dict[str, Any]:
    """POST `body` as JSON with a Bearer token; return the parsed JSON object.

    Raises urllib.error.HTTPError for non-2xx (caller turns it into an
    isError result with the server's message).
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — controlled URL
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def _dispatch_live(ref: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Live mode: create a one-node workflow on the real Aakaar API and start
    a run. Returns an MCP result containing {workflow_id, run_id, status}.

    All failure paths return isError results (never raise into the loop).
    """
    api = os.environ.get("AAKAAR_API", "").strip().rstrip("/")
    token = os.environ.get("AAKAAR_TOKEN", "").strip()
    if not api or not token:
        return _text_result(
            "live mode requires AAKAAR_API (base URL) and AAKAAR_TOKEN (bearer) "
            "to be set in the environment.",
            is_error=True,
        )

    kind = _infer_kind(ref)
    # Build the minimal single-node DAG the interpreter understands.
    dag = {
        "nodes": [{"id": "n1", "kind": kind, "ref": ref, "inputs": arguments}],
        "edges": [],
    }

    try:
        # 1) Create the workflow. Response is a WorkflowResponse whose `id`
        #    is the workflow id.
        wf = _http_json(
            f"{api}/workflows",
            {"name": f"mcp:{ref}", "description": "", "dag": dag, "rationale": "mcp"},
            token,
        )
        workflow_id = wf.get("id")
        if not workflow_id:
            return _text_result(
                f"workflow creation returned no id: {json.dumps(wf)[:500]}",
                is_error=True,
            )

        # 2) Start a run. CRITICAL: Current Aakaar's RunStartRequest is
        #    {version?, inputs?, target?} with extra=forbid. Sending
        #    `executor_type` (as older code did) yields a 422. We send the
        #    minimal valid body — an empty object — which defaults to running
        #    each node on its own target.
        run = _http_json(f"{api}/workflows/{workflow_id}/runs", {}, token)
        run_id = run.get("id")
        status = run.get("status")
        return _text_result(
            json.dumps(
                {"workflow_id": workflow_id, "run_id": run_id, "status": status},
                indent=2,
                default=str,
            )
        )
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")
        except Exception:
            pass
        return _text_result(
            f"Aakaar API error {exc.code} {exc.reason}: {detail[:1000]}",
            is_error=True,
        )
    except urllib.error.URLError as exc:
        return _text_result(
            f"could not reach Aakaar API at {api!r}: {exc.reason}", is_error=True
        )
    except Exception as exc:  # noqa: BLE001
        return _text_result(f"live dispatch failed: {exc}", is_error=True)


def _handle_tools_call(params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a tools/call: resolve the munged name to a ref and branch on
    AAKAAR_MCP_MODE. Tool-execution failures are returned as isError results,
    not JSON-RPC errors.
    """
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _text_result("`arguments` must be an object", is_error=True)

    _load_tools()
    defn = _BY_NAME.get(name)
    if defn is None:
        # Signaled to the caller as a JSON-RPC -32602 (handled in dispatch).
        raise _UnknownTool(name)

    ref = defn.ref
    mode = os.environ.get("AAKAAR_MCP_MODE", "describe").strip().lower()
    if mode == "live":
        return _dispatch_live(ref, arguments)
    return _describe(ref, arguments)


class _UnknownTool(Exception):
    """Raised when tools/call names a tool we don't expose."""

    def __init__(self, name: Any) -> None:
        super().__init__(str(name))
        self.name = name


# ---------------------------------------------------------------------------
# JSON-RPC method dispatch
# ---------------------------------------------------------------------------


def _handle_initialize(params: dict[str, Any]) -> dict[str, Any]:
    # Echo the client's protocolVersion when present so negotiation succeeds;
    # otherwise advertise ours.
    client_version = (params or {}).get("protocolVersion") or PROTOCOL_VERSION
    return {
        "protocolVersion": client_version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def _dispatch(method: str, params: dict[str, Any], req_id: Any) -> None:
    """Route one *request* (has an id) to its handler and reply."""
    if method == "initialize":
        _reply(req_id, result=_handle_initialize(params))
    elif method == "ping":
        _reply(req_id, result={})
    elif method == "tools/list":
        try:
            tools = _load_tools()
        except Exception as exc:  # noqa: BLE001
            _log("tools/list failed:", exc, "\n", traceback.format_exc())
            _reply(req_id, error=_rpc_error(-32603, f"failed to load tools: {exc}"))
            return
        _reply(req_id, result={"tools": tools})
    elif method == "tools/call":
        try:
            result = _handle_tools_call(params or {})
        except _UnknownTool as exc:
            _reply(req_id, error=_rpc_error(-32602, f"unknown tool: {exc.name!r}"))
            return
        except Exception as exc:  # noqa: BLE001
            # An unexpected handler failure: surface as a tool error result,
            # not a protocol error (per MCP semantics).
            _log("tools/call crashed:", exc, "\n", traceback.format_exc())
            _reply(req_id, result=_text_result(f"tool execution error: {exc}", is_error=True))
            return
        _reply(req_id, result=result)
    else:
        _reply(req_id, error=_rpc_error(-32601, f"unknown method: {method!r}"))


def _handle_line(line: str) -> None:
    """Parse and route a single newline-delimited input frame.

    Notifications (no `id`) are logged and ignored. Requests (with `id`) are
    dispatched. A non-JSON line that still exposes a parseable id gets a
    -32700; an unparseable line with no recoverable id is logged and skipped.
    """
    line = line.strip()
    if not line:
        return

    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        # Best-effort: try to recover an "id" so we can return a proper parse
        # error to a client waiting on a response.
        rid = _scavenge_id(line)
        if rid is not None:
            _reply(rid, error=_rpc_error(-32700, "parse error: invalid JSON"))
        else:
            _log("dropping unparseable line with no recoverable id")
        return

    if not isinstance(msg, dict):
        _log("dropping non-object JSON frame")
        return

    method = msg.get("method")
    has_id = "id" in msg

    # Notifications have no id; we never reply to them.
    if not has_id:
        if method:
            _log(f"notification: {method} (ignored)")
        else:
            _log("frame without method or id (ignored)")
        return

    if not isinstance(method, str):
        _reply(msg.get("id"), error=_rpc_error(-32600, "invalid request: missing method"))
        return

    _dispatch(method, msg.get("params") or {}, msg.get("id"))


def _scavenge_id(line: str) -> Any:
    """Try to pull a JSON-RPC id out of a line that failed to parse as JSON.

    Returns the id (int or str) or None. Purely best-effort so a malformed
    frame can still get a -32700 reply when the client is awaiting one.
    """
    marker = '"id"'
    idx = line.find(marker)
    if idx == -1:
        return None
    rest = line[idx + len(marker):]
    rest = rest.lstrip()
    if not rest.startswith(":"):
        return None
    rest = rest[1:].lstrip()
    if not rest:
        return None
    if rest[0] == '"':  # string id
        end = rest.find('"', 1)
        if end == -1:
            return None
        return rest[1:end]
    # numeric id: read leading digits
    num = ""
    for ch in rest:
        if ch.isdigit() or (ch == "-" and not num):
            num += ch
        else:
            break
    if num and num != "-":
        try:
            return int(num)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Read newline-delimited JSON-RPC from stdin until EOF, replying on
    stdout. Returns a process exit code."""
    _log(
        f"starting (protocol={PROTOCOL_VERSION}, "
        f"include={os.environ.get('AAKAAR_MCP_INCLUDE', 'capabilities')!r}, "
        f"mode={os.environ.get('AAKAAR_MCP_MODE', 'describe')!r})"
    )
    try:
        for line in sys.stdin:
            try:
                _handle_line(line)
            except Exception as exc:  # noqa: BLE001 — never let one frame kill the loop
                _log("unhandled error processing frame:", exc, "\n", traceback.format_exc())
    except KeyboardInterrupt:
        pass
    _log("stdin closed; exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
