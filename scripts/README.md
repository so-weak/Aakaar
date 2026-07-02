# scripts/

Per-component **start/stop** scripts for the runtime services, split by platform:

| Folder | Platform | Language |
|--------|----------|----------|
| [`mac/`](mac/) | macOS / Linux | Bash (`*.sh`) |
| [`windows/`](windows/) | Windows | PowerShell (`*.ps1`) + `*.cmd` launchers |

Both sets are 1:1 equivalents. Unlike the repo-root `dev.sh` / `start.sh` (which
open GUI Terminal tabs), these run each service **detached in the background**
with a pidfile + logfile, so they work over SSH / headless and can be stopped by
a separate script.

Standalone admin utilities that have no Windows counterpart stay at the
`scripts/` root: `reset-superuser-passwords.sh`, `rename-user-email.sh`,
`server-side-setup.sh`.

| Service | Start (mac) | Start (windows) | Stop | Port | Source dir |
|---------|-------------|-----------------|------|------|------------|
| **server** (FastAPI API) | `mac/start-server.sh` | `windows\start-server.ps1` | `stop-server.*` | 8000 | `aakaar/` |
| **client** (Vite web console) | `mac/start-client.sh` | `windows\start-client.ps1` | `stop-client.*` | 5173 | `aakaar-web/` |
| **broker** (WebSocket relay) | `mac/start-broker.sh` | `windows\start-broker.ps1` | `stop-broker.*` | 9300 | `aakaar-broker/` |
| **agent** (remote executor) | `mac/start-agent.sh` | `windows\start-agent.ps1` | `stop-agent.*` | none (dials out) | `aakaar-agent/` |

Bonus: `start-all` and `stop-all` run the server-side trio in dependency order
(broker → server → client, and the reverse to stop). The **agent** is the *other
side* of the connection and is managed on its own (it needs an enrollment key and
a server to dial), so it is **not** part of `start-all`.

## Windows quick start

```powershell
# from the repo root, in PowerShell:
scripts\windows\start-all.ps1
scripts\windows\stop-all.ps1

# the agent (run it on the machine Aakaar should drive):
$env:AAKAAR_AGENT_SERVER = 'ws://YOUR-SERVER:8000'
$env:AAKAAR_AGENT_KEY    = '<id>.<secret>'
scripts\windows\start-agent.ps1
```

`.cmd` launchers exist for the four entry points (`start-all`, `stop-all`,
`start-agent`, `stop-agent`) — double-click them, or run from `cmd.exe`. They
invoke PowerShell with `-ExecutionPolicy Bypass`, so you don't have to relax the
machine's execution policy. Set env vars first with `set VAR=value` in `cmd`, or
`$env:VAR='value'` in PowerShell.

## Behaviour

- **First run bootstraps deps**: server/broker create their `.venv`, the server
  also installs Playwright Chromium and runs DB migrations, the client runs
  `npm install`. Later runs skip all of that.
- **Detached**:
  - *mac* — each service is `nohup`-ed; pid → `.run/<svc>.pid`, combined
    stdout+stderr → `.run/<svc>.log` (follow with `tail -f`).
  - *windows* — each service is launched in a hidden window via `Start-Process`
    (survives closing the terminal); pid → `.run\<svc>.pid`, stdout →
    `.run\<svc>.log` and stderr → `.run\<svc>.err.log` (two files — `Start-Process`
    can't merge them; follow with `Get-Content -Wait`). Logs are truncated per
    start rather than appended.
- **Idempotent start**: refuses to double-start if the pid is alive or the port
  is already taken.
- **Robust stop**:
  - *mac* — SIGTERM the recorded pid (→ SIGKILL after a grace period), then free
    the service's port as a fallback (catches reload/Vite children).
  - *windows* — `taskkill /T /F` the recorded pid tree (Windows console apps don't
    reliably honour a graceful signal), then free the port as a fallback.
- **Shared runtime state** lives in `scripts/.run/` (gitignored) for *both*
  platforms — so the broker token stays in one place.

## Broker pairing

The broker needs a shared secret (`AAKAAR_BROKER_TOKEN`) that the **server must
also use**. `start-broker` generates one if you don't supply it and saves it to
`.run/broker.token`. Wire the server to the broker either way:

```bash
# mac — easiest: server reads .run/broker.token automatically:
AAKAAR_USE_LOCAL_BROKER=1 scripts/mac/start-server.sh
```

```powershell
# windows — same idea:
$env:AAKAAR_USE_LOCAL_BROKER = '1'; scripts\windows\start-server.ps1
```

Or set it explicitly (e.g. in `aakaar/.env`): `AAKAAR_BROKER_URL=ws://127.0.0.1:9300`
and `AAKAAR_BROKER_TOKEN=<token from .run/broker.token>`.

`start-all` enables `AAKAAR_USE_LOCAL_BROKER=1` by default.

## Common env knobs

Same variable names on both platforms; only the syntax to set them differs
(`VAR=val cmd` on mac, `$env:VAR='val'` on windows).

| Variable | Applies to | Default |
|----------|-----------|---------|
| `AAKAAR_API_HOST` / `AAKAAR_API_PORT` | server | `0.0.0.0` / `8000` |
| `AAKAAR_RELOAD=0` | server (disable `--reload`) | `1` |
| `AAKAAR_WEB_HOST` / `AAKAAR_WEB_PORT` | client | vite default / `5173` |
| `AAKAAR_BROKER_HOST` / `AAKAAR_BROKER_PORT` | broker | `0.0.0.0` / `9300` |
| `AAKAAR_BROKER_TOKEN` | broker + server | generated & persisted |
| `AAKAAR_AGENT_SERVER` | agent (server/broker base URL) | `ws://127.0.0.1:8000` |
| `AAKAAR_AGENT_KEY` | agent (**required** enrollment key) | — |
| `AAKAAR_AGENT_EXTRAS` | agent (pip extras, e.g. `gui,record`) | none |
| `AAKAAR_PYTHON` | venv bootstrap | `python3` (mac) / `python` (windows) |
| `AAKAAR_WAIT` | port-up wait (s) | `30` |

## Remote agent (this machine driven by a server elsewhere)

The agent runs on the workstation you want Aakaar to automate and **dials out**
to a server/broker on another machine — it has no inbound port. Typical split:
the **server + client + broker run on one host**, the **agent runs here**.

1. On the server host, enroll an agent (web console → Agents, or
   `POST /agents/enroll`) to get an `<id>.<secret>` key.
2. On this machine, point the agent at the server and start it:

   ```bash
   # mac / linux
   AAKAAR_AGENT_SERVER=ws://SERVER-HOST:8000 \
   AAKAAR_AGENT_KEY=<id>.<secret> \
   scripts/mac/start-agent.sh
   # …or go through the broker:  AAKAAR_AGENT_SERVER=ws://BROKER-HOST:9300
   # …stop with:                 scripts/mac/stop-agent.sh
   ```

   ```powershell
   # windows
   $env:AAKAAR_AGENT_SERVER = 'ws://SERVER-HOST:8000'
   $env:AAKAAR_AGENT_KEY    = '<id>.<secret>'
   scripts\windows\start-agent.ps1
   # …stop with:  scripts\windows\stop-agent.ps1
   ```

   Tip: drop `AAKAAR_AGENT_SERVER` / `AAKAAR_AGENT_KEY` into `aakaar-agent/.env`
   and just run the start script. For desktop/RPA capabilities add
   `AAKAAR_AGENT_EXTRAS=gui,record` on the first run (installs the extra deps).

A wrong key or unreachable server shows up in `.run/agent.log` (the agent keeps
retrying), not as a start-time failure — watch it with `tail -f .run/agent.log`
(mac) or `Get-Content -Wait .run\agent.log` (windows).
