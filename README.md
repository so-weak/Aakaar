# Aakaar

**Multi-tenant, natural-language → DAG workflow automation.**

You describe a task in plain language; an LLM compiles it into a typed **DAG** of
registry-defined **capabilities**; the runtime executes that DAG. Capabilities are
staff-authored and **tenant-granted**, credentials live in a **vault** (never in
chat, never in the DAG), and any step can run on the server **or be dispatched to a
remote agent on another machine**.

> The LLM only ever emits a DAG. The runtime is a generic interpreter — it has no
> opinion about any specific workflow. Add a capability, grant it, and it's live.

---

## How it works

```
  "log into the portal and download last month's statement on the branch PC"
                              │
                              ▼
                    ┌──────────────────┐
                    │  Planner (LLM)   │   restricted to the tenant's granted
                    │  NL → DAG        │   capabilities; asks to clarify or
                    └────────┬─────────┘   reports what's missing — never guesses
                             ▼
   ┌──────────────────── Aakaar server (the brain) ─────────────────────┐
   │  RunOrchestrator ─▶ LocalExecutor                                   │
   │                       per node:  control            → server        │
   │                                  target = server    → local handler │
   │                                  target = <agent>   → RemoteDispatcher
   │     EventRecorder ─▶ /ws/runs/{id}        AgentRegistry  │ /ws/agents │
   └─────────────────────────────────────────────────────────│──────────┘
                          agent dials OUT over WebSocket  ◀────┘
              ┌──────────── Remote agent (the hands) ──────────────┐
              │  Win / macOS / Linux workstation                    │
              │  runs shell / system / desktop-GUI capabilities     │
              └─────────────────────────────────────────────────────┘
```

The **server orchestrates**; an **agent is a pair of hands** on a specific machine.
See [documentation/remote-execution-architecture.md](documentation/remote-execution-architecture.md)
for the full design.

---

## Repository layout

| Path | What it is |
|---|---|
| [aakaar/](aakaar/) | The server: API (FastAPI), planner (NL→DAG), DAG interpreter / `LocalExecutor`, capability registry, vault, remote-dispatch spine. |
| [aakaar-agent/](aakaar-agent/) | The standalone **remote execution agent** — a lightweight process deployed to a workstation. Dials out to the server and runs dispatched capability nodes. |
| [aakaar-capabilities/](aakaar-capabilities/) | The **shared capability library** (`aakaar_caps`). A capability written once here runs on the server *or* an agent. |
| [aakaar-web/](aakaar-web/) | The operator web UI (Vite + React + TypeScript). |
| [documentation/](documentation/) | Architecture notes. |

The default DB is SQLite, the vector store is Chroma (for capability search), the
object store and vault are local-filesystem. Coordination is in-process — **no
Redis, no external broker, no Temporal**.

> This workspace also vendors `admin-app/` and `nbbl-app/`, which are separate
> co-located apps, not part of Aakaar proper.

---

## Quickstart

**Prerequisites:** Python 3.11+, Node 18+, and a `tesseract` binary only if you use
OCR capabilities.

```bash
# Required — the server refuses to start without a JWT secret:
export AAKAAR_JWT_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"

# Needed for the NL→DAG planner (the runtime itself does not call the LLM):
export OPENAI_API_KEY="sk-..."

./start.sh
```

`start.sh` is idempotent: it bootstraps a virtualenv per package (installing the
server *and* the shared capability library editable), installs the Playwright
Chromium browser, runs DB migrations, and launches each service in its own terminal:

| Service | URL |
|---|---|
| Aakaar API | http://localhost:8000 |
| Aakaar web UI | http://localhost:5173 |

Stop everything with `./kill.sh`.

### Running the tests

```bash
# Server suite
cd aakaar && .venv/bin/python -m pytest

# Agent suite (uses the shared lib from the server venv)
cd aakaar-agent && PYTHONPATH=. ../aakaar/.venv/bin/python -m pytest
```

---

## Running a workflow locally vs. on another system

Placement is a property of the DAG, resolved at run time:

- **On the server (default)** — a node with `target = "server"` (or unset) runs in
  the server's in-process executor.
- **On a remote machine** — set a node's `target` to an agent alias/pool, or choose
  a **"Run on"** target at launch to send the *whole* workflow to one agent. Control
  nodes always stay on the server as the orchestrator; capability nodes are shipped
  to the agent over the WebSocket, executed there, and their results returned — with
  just-in-time credentials, a per-task deadline, audit, and a "ran on `<agent>`"
  badge on the run timeline.

This is what lets you **author once and trigger the same workflow onto any enrolled
workstation from your machine.**

---

## The capability model

Capabilities are split by *where they can run*:

| Kind | Examples | Runs on |
|---|---|---|
| **Shared** (`aakaar_caps`) | `cap.shell_exec`, `cap.system_info`, `cap.json_extract` | **server _or_ agent** — written once, portable |
| **Server-only** | browser automation (Playwright), SFTP, document / data / image processing | the server (needs heavy/local services) |
| **Agent-only** (GUI) | `cap.desktop_click`, `cap.desktop_type`, `cap.clipboard_write`, `cap.window_manage` | a remote agent with an interactive desktop session |

The server knows the contracts for **all** capabilities (so the planner and editor
can use them); an agent only runs the subset it advertises. A GUI node is therefore
**never silently placed on a headless agent** — placement fails fast instead.

---

## Deploying a remote agent

The agent is **lightweight** — its only dependencies are `websockets`, `httpx`,
`psutil`, and `pydantic` (+ an optional `gui` extra). It contains **no Playwright,
no browser, and none of the server's ML/document stack** — those capabilities stay
on the server. A built agent is ~40 MB, with no per-machine browser download.

1. **Enroll** (tenant-admin): the **Agents** page, or `POST /agents/enroll`. You get
   a one-time enrollment key `"<agent_id>.<secret>"` (only a bcrypt hash is stored).

2. **Install & run** on the target machine:

   ```bash
   cd aakaar-agent
   python3 -m venv .venv
   .venv/bin/pip install -e . -e ../aakaar-capabilities   # add '.[gui]' for desktop caps
   .venv/bin/aakaar-agent --server ws://<server-host>:8000 --key '<agent_id>.<secret>'
   ```

   The agent dials **out** (no inbound ports), advertises its OS / GUI session /
   capabilities, and serves dispatched tasks until stopped. For a quick local dev
   agent, `AAKAAR_START_AGENT=1 AAKAAR_AGENT_KEY='<id>.<secret>' ./start.sh`.

3. **Headless vs. GUI.** Shell / system / JSON capabilities run anywhere. The
   desktop-GUI capabilities need an **interactive session** and OS permissions:
   - **macOS** — run as a *LaunchAgent* (user session) + grant Accessibility +
     Screen Recording. A LaunchDaemon is headless-only.
   - **Windows** — run GUI caps from a user-session process (logon task), not a
     Session-0 service.
   - **Linux** — needs `DISPLAY`/Wayland in a user session (X11 preferred).

   Signed per-OS binaries + service installers are on the roadmap; today the agent is
   installed via Python as above.

---

## Configuration

**Server** (read from the environment at startup):

| Variable | Purpose |
|---|---|
| `AAKAAR_JWT_SECRET` | **Required.** Signs access tokens; the server won't start without it. |
| `OPENAI_API_KEY` | LLM access for the NL→DAG planner. |
| `AAKAAR_DB_URL` | Primary DB (default `sqlite:///<data>/aakaar.sqlite`). |
| `AAKAAR_DATA_DIR` | Object store / vault / caches (default `./data`). |
| `AAKAAR_REMOTE_EXEC_ENABLED` | Enable remote dispatch (default `true`; inert with no agents). |
| `AAKAAR_REMOTE_TASK_TIMEOUT_SECONDS` | Per-remote-task deadline (default `300`). |

**Agent:**

| Variable / flag | Purpose |
|---|---|
| `--server` / `AAKAAR_AGENT_SERVER` | Base WebSocket URL of the server (e.g. `ws://host:8000`); `/ws/agents` is appended. |
| `--key` / `AAKAAR_AGENT_KEY` | The enrollment key `"<agent_id>.<secret>"`. |
| `AAKAAR_AGENT_HEADLESS` | Force headless (skip GUI capabilities) on macOS. |
| `AAKAAR_AGENT_LOG_LEVEL` | Log level (default `INFO`). |

---

## Further reading

- [documentation/remote-execution-architecture.md](documentation/remote-execution-architecture.md) — the full remote-execution design: placement, wire protocol, security, cross-OS strategy, durability.
- [aakaar/README.md](aakaar/README.md) — server package.
