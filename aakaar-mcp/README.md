# aakaar-mcp

An [MCP](https://modelcontextprotocol.io/) (Model Context Protocol) **stdio
server** that projects the **Aakaar capability registry** as MCP tools. Any
MCP-aware agent (Claude Desktop, etc.) can then discover Aakaar's capabilities
and either *describe* what running one would do or actually *run* it against a
live Aakaar deployment.

It is a sibling package to the backend (`../aakaar/`). It **reads** the
`aakaar` package to build the registry but never modifies it. The server
runtime is **stdlib-only** — no third-party MCP SDK.

---

## How it works

On `tools/list`, the server builds a fully-loaded registry exactly like the app
does:

```python
from aakaar.shared.registry import build_default_registry
from aakaar.interpreter.activities.registry import ActivityRegistry
from aakaar.capabilities import load_into

reg = build_default_registry()
load_into(reg, ActivityRegistry())
```

Each registry `Definition` becomes one MCP tool:

- **name** = `ref.replace(".", "_")` (see *Name munging* below)
- **description** = `"[<ref>] <description>"`, plus, for capabilities with
  secrets, a line naming the required secret names so an agent knows a tenant
  **grant** is a prerequisite (names only — never values).
- **inputSchema** = the input pydantic model's `model_json_schema()`
- **outputSchema** = the output pydantic model's `model_json_schema()`
  (an improvement over the reference projection — lets agents shape-check
  results)

A `by_name` map (munged name → real `Definition`) lets `tools/call` recover the
original dotted ref.

### Name munging

MCP tool names may not contain dots, but Aakaar refs are dotted
(`cap.db_query`, `browser.navigate`). We map `.` → `_`, so `cap.db_query`
becomes the tool `cap_db_query`. The reverse lookup is done via the `by_name`
table, not by un-munging.

**Collision guard:** if two distinct refs ever munge to the same name, the
server logs a warning and appends a numeric suffix (`_2`, `_3`, …) instead of
silently overwriting one of them.

---

## Modes & environment variables

| Variable             | Default          | Meaning                                                                 |
|----------------------|------------------|-------------------------------------------------------------------------|
| `AAKAAR_MCP_INCLUDE` | `capabilities`   | `capabilities` = only `kind == "capability"`; `all` = also actions + controls |
| `AAKAAR_MCP_MODE`    | `describe`       | `describe` = side-effect-free plan; `live` = hit the real Aakaar API     |
| `AAKAAR_API`         | *(unset)*        | Base URL of the Aakaar HTTP API (**live mode only**), e.g. `http://127.0.0.1:8000` |
| `AAKAAR_TOKEN`       | *(unset)*        | Bearer token for the Aakaar API (**live mode only**)                    |

### Describe mode (default — safe)

`tools/call` returns a JSON **plan** describing what *would* run
(`{channel, capability_ref, arguments, execution, note}`). **No side effects.**
Great for letting an agent reason about a capability without touching anything.

### Live mode

`tools/call` reaches the real Aakaar API via stdlib `urllib.request`:

1. Infers the node kind from the ref prefix: `cap.*` → `capability`;
   `control.*` / `human.prompt` → `control`; everything else → `action`.
2. Builds a one-node DAG:
   `{"nodes": [{"id": "n1", "kind": <kind>, "ref": <ref>, "inputs": <arguments>}], "edges": []}`.
3. `POST {AAKAAR_API}/workflows` with
   `{"name": "mcp:<ref>", "description": "", "dag": <dag>, "rationale": "mcp"}`
   (Bearer auth) → workflow id.
4. `POST {AAKAAR_API}/workflows/{id}/runs` with body `{}` → run id + status.

It returns `{workflow_id, run_id, status}`.

> **Current-Aakaar run-start body.** Current Aakaar's `RunStartRequest` is
> `{version?, inputs?, target?}` with `extra="forbid"`. **Do not** send
> `executor_type` — it will 422. We send `{}` (you could send `{"target":
> "server"}` to pin everything to the API host). Missing `AAKAAR_API` /
> `AAKAAR_TOKEN` returns a clean `isError` result, not a `KeyError`.

---

## stdout quarantine (why logs go to stderr)

This is a **JSON-RPC over stdio** server: stdout carries one JSON frame per
line and **nothing else**. A single stray `print()` from any transitively-
imported dependency would land in the middle of a frame and corrupt the stream,
breaking the client. To prevent that:

- `_log()` writes **only to stderr**.
- Every protocol response goes through one `_reply()` that writes `json + "\n"`
  to stdout and flushes.
- During the import-heavy registry build, the server temporarily redirects
  `sys.stdout` to `sys.stderr` (restored in a `finally`), so any import-time
  print is captured harmlessly on stderr.

---

## Wiring into Claude Desktop

Merge `claude_desktop_config.snippet.json` into your Claude Desktop config
(`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS).
The snippet runs the server with the venv interpreter that can import
`aakaar`:

```json
{
  "mcpServers": {
    "aakaar": {
      "command": "/Users/soubhikghosh/Codes/Aakaar/aakaar/.venv/bin/python",
      "args": ["/Users/soubhikghosh/Codes/Aakaar/aakaar-mcp/server.py"],
      "env": {
        "AAKAAR_MCP_INCLUDE": "capabilities",
        "AAKAAR_MCP_MODE": "describe",
        "AAKAAR_API": "http://127.0.0.1:8000",
        "AAKAAR_TOKEN": "REPLACE_WITH_BEARER_TOKEN"
      }
    }
  }
}
```

Use the interpreter from the Aakaar venv (`aakaar/.venv/bin/python`) so the
`aakaar` package is importable. For describe mode you can drop `AAKAAR_API` /
`AAKAAR_TOKEN` entirely.

---

## Running locally

```bash
# Compile-check the server (no aakaar import needed):
python -m py_compile server.py inspect_tools.py

# Smoke test: spawn the server, handshake, list tools, do a describe call.
# Use the venv that can import aakaar:
/Users/soubhikghosh/Codes/Aakaar/aakaar/.venv/bin/python inspect_tools.py

# Call a specific tool by its munged name:
/Users/soubhikghosh/Codes/Aakaar/aakaar/.venv/bin/python inspect_tools.py cap_db_query
```

If installed as a package (`pip install -e .`), the console script
`aakaar-mcp` runs `server:main`.

---

## JSON-RPC methods

| Method        | Notes                                                                 |
|---------------|-----------------------------------------------------------------------|
| `initialize`  | Echoes the client's `protocolVersion`; advertises `tools` capability + `serverInfo`. |
| `ping`        | Returns `{}`.                                                         |
| `tools/list`  | Returns the projected tools.                                          |
| `tools/call`  | Describe or live, per `AAKAAR_MCP_MODE`.                              |
| *notifications* | Frames with no `id` (e.g. `notifications/initialized`) are logged + ignored. |

**Error handling**

- Unknown method → `-32601`.
- Unknown tool in `tools/call` → `-32602`.
- A non-JSON input line that still exposes a parseable `id` → `-32700`
  (if no id is recoverable, the line is logged and skipped).
- **Tool-execution failures are not JSON-RPC errors** — they return
  `{"content": [...], "isError": true}` with the message in `content`.

---

## Caveats (honest)

- **No input validation against the schema.** `tools/call` forwards the
  agent's `arguments` straight into the DAG node. The Aakaar API validates on
  its side, so bad inputs surface as a 422 in live mode — but the MCP layer
  itself does not pre-validate.
- **Live mode is genuinely live.** It creates a real workflow and starts a
  real run under whatever tenant/user the `AAKAAR_TOKEN` belongs to. There is
  no dry-run confirmation step. Keep the default `describe` mode unless you
  mean it.
- **Grants are not checked here.** A capability tool will appear in
  `tools/list` even if the calling tenant has no grant for it; the run will
  fail at the API/validation layer instead. The secret-name hint in the
  description is informational only.
- **Fire-and-return, no polling.** Live mode returns the run id and initial
  status; it does not wait for completion, stream events, or fetch outputs.
  Use the Aakaar API/UI to follow the run.
- **Single-node DAGs only.** Each tool call maps to exactly one node. Multi-
  step composition is out of scope for this projection — that's what the
  Aakaar planner is for.
- **`outputSchema` support varies by client.** Some MCP clients ignore it.
  It's emitted as a best-effort improvement.
- **Stdout discipline depends on import behavior.** The quarantine handles
  prints during the registry build. A dependency that prints *after* the build
  (e.g. from a background thread) could still, in theory, interleave — none of
  the current imports do this, but it's a known limitation of stdio MCP in
  general.
- **Interpreter coupling.** The server must run under an interpreter where
  `import aakaar` works (the Aakaar venv, or a `pip install -e .`'d env). It is
  not standalone in that sense.
