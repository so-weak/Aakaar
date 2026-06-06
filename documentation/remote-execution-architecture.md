# Aakaar Remote Execution — Architecture

Status: backend spine, agent package, and frontend surfaces implemented and
tested in-tree. Hardware-gated pieces (real GUI automation on a live desktop
session, signed per-OS installers, mutual-TLS certificate issuance) are
implemented behind the contract and unit-tested with skips; they are called out
explicitly under **Hardening roadmap**.

## 1. Purpose
Most workflow steps run inside the server's in-process `LocalExecutor`. Some
cannot or should not:
- **GUI / desktop automation** — driving native apps needs a machine with a
  logged-in display and the app installed.
- **Locality** — a LAN-only intranet app, a local database, a file share, or
  hardware reachable only from a specific workstation.
- **Data residency** — data that must never leave the workstation.

Remote execution lets *individual DAG nodes* run on a designated agent
(workstation) while the server continues to orchestrate the run. **The server is
the brain; an agent is a pair of hands.**

## 2. Constraints (these shape every decision)
- Airgapped server; its only outbound connection is the OpenAI SDK. Agents sit
  on the same airgapped LAN, so agent↔server traffic is LAN-internal.
- SQLite is the only primary DB; Chroma is the vector DB. **No Redis / external
  broker / Temporal** — coordination is in-process or a SQLite row.
- Local-filesystem object store; local-file secret vault.
- Single-node server; in-process async executor with per-node retries and
  crash-safe recovery (interrupted runs are marked FAILED on restart).
- Must support Windows, macOS, and Linux agents.

## 3. Design at a glance
Placement is a property of the **node**, dispatch happens **through the
executor**, execution happens on a **tenant-scoped agent over a WebSocket the
agent dials out**.

```
            ┌──────────────────────── Aakaar server (single node) ───────────────────────┐
 DAG ─────▶ │ RunOrchestrator ─▶ LocalExecutor                                            │
 (nodes      │                      │  per-node: control? → server                         │
  carry a    │                      │            target=server/None → local handler        │
  target)    │                      │            target=<agent/pool> → RemoteDispatcher ───┐ │
            │                      ▼                                                     │ │
            │   EventRecorder + in-proc broker → /ws/runs/{id}   AgentRegistry (in-mem) │ │
            └────────────────────────────────────────────────────────────│─────────────┘ │
                                                                          │  /ws/agents    │
                                              mTLS-ready WebSocket (agent dials out) ▼     │
                                   ┌──────────── Remote agent (Win/macOS/Linux) ───────────┘
                                   │ client loop → capability handlers (shell/system/desktop/…)
                                   └────────────────────────────────────────────────────────
```

## 4. Components

### Server side
| Concern | Where |
|---|---|
| Node placement field + validator (control nodes stay on server) | `aakaar/shared/dag/types.py` (`Node.target`, `TARGET_RE`), `aakaar/shared/dag/validator.py` |
| Durable agent record (`(tenant_id, alias)` unique) | `aakaar/db/models.py` (`RemoteAgent`), migration `0004_remote_agents.py`, repo `aakaar/api/repositories/agents.py` |
| Wire contract + connection abstraction | `aakaar/workers/remote/protocol.py` (`AgentInfo`, `RemoteTask`, `RemoteResult`, `AgentConnection`) |
| In-memory live registry + placement resolution | `aakaar/workers/remote/registry.py` (`AgentRegistry`, `NoAgentAvailable`) |
| WebSocket / fake connection | `aakaar/workers/remote/connection.py` (`WebSocketAgentConnection`, `FakeAgentConnection`) |
| Dispatch (resolve → credential envelope → deadline → audit/provenance → map result) | `aakaar/workers/remote/dispatcher.py` (`RemoteDispatcher`, `RemoteExecError`) |
| Pre-flight placement check | `aakaar/workers/remote/placement.py` (`check_placement`) |
| Executor hook | `aakaar/interpreter/executor.py` (`LocalExecutor.remote_dispatcher`, branch in `_dispatch`) |
| Agent REST (enroll/list/revoke) + `/placement/check` + `/ws/agents` | `aakaar/api/routers/agents.py` |
| Remote capability **contracts** (definition-only, no local handler) | `aakaar/capabilities/remote/*` + loader `remote_only` support in `aakaar/capabilities/_base.py` |
| Wiring + config | `aakaar/api/deps.py` (`agent_registry`, `remote_dispatcher`), `aakaar/core/config.py` (`remote_exec_enabled`, `remote_task_timeout_seconds`) |

### Agent side (`aakaar-agent/`, a standalone deployable — minimal deps)
| Concern | Where |
|---|---|
| Entry + config (flags / `AAKAAR_AGENT_SERVER`, `AAKAAR_AGENT_KEY`) | `aakaar_agent/main.py` |
| Connection loop (dial out, hello, serve tasks concurrently, reconnect) | `aakaar_agent/client.py` |
| OS + GUI-session detection | `aakaar_agent/session.py` |
| Capability framework (discover/advertise/dispatch) | `aakaar_agent/capabilities/__init__.py` |
| Handlers — headless | `shell_exec.py`, `system_info.py` |
| Handlers — GUI (lazy, require a session) | `desktop_click.py`, `desktop_type.py`, `clipboard_write.py`, `window_manage.py` |

## 5. Wire protocol (JSON over the agent WebSocket)
```
agent → server  hello    {type, os, gui, version, hostname, capabilities:[{ref,version}]}
server → agent  welcome  {type, alias}                      # registration confirmed
server → agent  task     {type, task_id, run_id, node_id, ref, inputs, secrets, timeout_s}
agent → server  result   {type, task_id, ok, outputs? | error?}
agent → server  event    {type, run_id, node_id, kind, payload}   # optional progress
both            ping/pong (WebSocket frames)                       # liveness
```
`secrets` is the **credential envelope**: only the values a node needs, fetched
just-in-time from the vault and never persisted on the agent.

## 6. End-to-end flow
1. A DAG node carries `target` = an agent alias or pool (set in the editor; or
   implied by a remote-only capability).
2. `RunOrchestrator` drives the run; `LocalExecutor` walks layers as usual.
3. For a remote-targeted node, `_dispatch` calls `RemoteDispatcher.run(...)`.
4. The dispatcher resolves an online agent via `AgentRegistry` (tenant + pool/
   alias + capability/version + GUI requirement), builds a `RemoteTask`
   (resolved inputs + credential envelope), and `await`s it under a deadline.
5. The agent runs the capability handler and returns a `RemoteResult`.
6. The dispatcher maps outputs back (or raises, so the executor's normal
   failure/retry path applies), audits which agent ran it, and emits a run LOG
   event (`payload.agent`) so the timeline shows provenance for any viewer.
7. Events flow through the same recorder → broker → `/ws/runs/{id}`, so remote
   steps light up the UI identically to local ones.

## 7. Placement model
- `Node.target`: `null`/`"server"` = local (default, unchanged behavior);
  otherwise a selector matching `^(server|[a-z][a-z0-9_:\-]{0,63})$` — an exact
  **agent alias** (wins) or a **pool label** (any matching online agent).
- Resolution filters by: same tenant, online, supports `ref`@version, and
  (when the capability is `gui`-tagged) `gui_capable`. A deterministic pick
  keeps placement stable/testable. No match → `NoAgentAvailable` with a reason.
- **Run-level placement (chosen at launch):** the run-start request and a
  schedule may carry an optional `target` that overrides per-node placement for
  the *whole* run — `null` = use each node's own target (default); `"server"` =
  run everything on the host; an agent/pool = run the whole workflow there
  (control nodes always stay on the server). It threads
  `RunStartRequest.target → RunOrchestrator.schedule(run_target=…) →
  RunContext.run_target →` the executor's effective-target choice. The UI offers
  it as a "Run on" selector in the launch modal (and the create-schedule form);
  per-node `target` set in the editor is the override when no run-level choice
  is made. `WorkflowSchedule.target` (migration `0005`) carries it for scheduled
  runs.
- `check_placement(dag, tenant)` runs the same logic without dispatching — it
  powers the editor's inline warnings and both the launch-time and run-level
  pre-flight gates.

## 8. Security
Implemented:
- **Tenant scoping** — agents are `(tenant_id, alias)`-unique; a run only ever
  reaches its own tenant's agents (fixes the classic global-alias leak).
- **Identity** — enrollment issues a one-time key `"<agent_id>.<secret>"`; only
  a bcrypt hash of the secret is stored; the agent authenticates on connect.
- **Credential envelope** — secrets are fetched just-in-time, sent only for the
  node that needs them, never persisted by the agent, never placed in DAG/run
  JSON.
- **Command-injection defense** — `shell_exec` takes `argv` (a list), never a
  shell string.
- **Audit** — enroll / revoke / every remote dispatch (which agent, ok/fail)
  are recorded in the audit log.

Hardening roadmap (designed-for, not yet implemented):
- **mutual TLS** with a per-deployment CA (don't trust "airgapped LAN = safe");
  reject plaintext in production. Today the transport is WS that should be
  terminated as `wss://` behind the proxy.
- Agent **config encryption at rest** (DPAPI / Keychain / file-perms+key).
- **Binary integrity** check at startup; auth **rate-limiting** on `/ws/agents`.
- Per-grant **command allowlists** for `shell_exec`.

## 9. Cross-OS strategy
The protocol is OS-agnostic; the hard part is the **GUI/session model**:
- **Windows** — a Session-0 service cannot touch the user desktop. Run headless
  capabilities as a service; run GUI capabilities from a user-session process
  (logon scheduled task). The agent reports `gui` from `SESSIONNAME`.
- **macOS** — GUI automation needs a **LaunchAgent** (user GUI session) plus TCC
  consent (Accessibility + Screen Recording); a LaunchDaemon is headless-only.
  Distribution needs signing + notarization.
- **Linux** — GUI needs `DISPLAY`/Wayland in a user session (`systemd --user`);
  a system unit is headless. Wayland blocks synthetic input — prefer X11 or
  degrade.

The agent advertises `os` + `gui_capable` + its `capability@version` set; the
placement resolver enforces them, so a GUI node is **never silently placed on a
headless agent** — it fails fast (or simply isn't scheduled there).
Packaging (roadmap): one PyInstaller binary per OS, deps bundled (offline), with
the appropriate service supervisor; GUI deps are an optional `gui` extra,
imported lazily so a headless agent never needs them.

## 10. Failure & durability (no Redis / no Temporal / single-node)
- **Task-level timeout** — every remote task has a deadline; a miss fails the
  node (then node-level retry applies if configured). This covers the
  "agent dies while the server lives" gap that node-retry + restart-recovery
  alone do not.
- **Heartbeat / disconnect** — WebSocket ping/pong; on disconnect the agent is
  deregistered and its in-flight tasks fail with a clear reason.
- **Server restart** — the existing `recover_interrupted_runs()` marks in-flight
  runs FAILED (consistent with the platform's durability stance).
- **Coordination** is the in-memory `AgentRegistry` over the live socket (safe
  on a single node); SQLite holds only durable agent metadata/status — no
  polled task queue.
- **Idempotency** — retries are per-node; remote nodes with side effects should
  be idempotent or not retried.

## 11. Data & artifacts
Today: node inputs/outputs travel as JSON in the task/result frames. Roadmap: a
separate streamed, tenant-scoped artifact channel (so large files don't inflate
the socket) and a **data-residency mode** that keeps artifacts on the agent and
returns only a reference.

## 12. Frontend surfaces
- **Agents page** (tenant-admin): fleet status + enroll (one-time key) + revoke.
- **DAG editor**: a "Runs on" selector per node + remote badges + inline
  placement warnings (via `/placement/check`).
- **Launch**: pre-flight placement gate before a run starts.
- **Run view**: a per-node "ran on `<agent>`" badge derived from the LOG event.
All additive and theme-faithful; invisible to tenants with no agents enrolled.

## 13. Testing
- `aakaar/tests/test_remote_execution.py` — validator rules, registry/placement
  resolution + tenant scoping, **end-to-end remote dispatch through the real
  executor via an in-process fake agent**, retry/failure propagation, mixed
  local+remote runs, the placement-check endpoint, the agents REST API, and a
  **real-WebSocket round trip** (`/ws/agents`: enrolled agent connects → hello →
  registered/online → offline on disconnect; bad key rejected).
- `aakaar-agent/tests/test_capabilities.py` — headless handlers (`shell_exec`,
  `system_info`) exercised for real; GUI handlers assert the graceful "needs the
  gui extra" contract (skipped where the optional deps are present).
- **Hardware-gated** (not runnable in CI here): real GUI automation on a live
  desktop, signed installers, mTLS cert issuance, true multi-OS runtime.

## 14. Configuration
- Server: `AAKAAR_REMOTE_EXEC_ENABLED` (default true; inert with no agents),
  `AAKAAR_REMOTE_TASK_TIMEOUT_SECONDS` (default 300).
- Agent: `--server`/`AAKAAR_AGENT_SERVER` (base WS URL), `--key`/
  `AAKAAR_AGENT_KEY`, `AAKAAR_AGENT_HEADLESS` (force headless on macOS),
  `AAKAAR_AGENT_LOG_LEVEL`.
