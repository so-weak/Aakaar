# scripts/

Per-component **start/stop** scripts for the three runtime services. Unlike the
repo-root `dev.sh` / `start.sh` (which open GUI Terminal tabs), these run each
service **detached in the background** with a pidfile + logfile, so they work
over SSH / headless and can be stopped by a separate script.

| Service | Start | Stop | Port | Source dir |
|---------|-------|------|------|------------|
| **server** (FastAPI API) | `./start-server.sh` | `./stop-server.sh` | 8000 | `aakaar/` |
| **client** (Vite web console) | `./start-client.sh` | `./stop-client.sh` | 5173 | `aakaar-web/` |
| **broker** (WebSocket relay) | `./start-broker.sh` | `./stop-broker.sh` | 9300 | `aakaar-broker/` |
| **agent** (remote executor) | `./start-agent.sh` | `./stop-agent.sh` | none (dials out) | `aakaar-agent/` |

Bonus: `./start-all.sh` and `./stop-all.sh` run the server-side trio in
dependency order (broker → server → client, and the reverse to stop). The
**agent** is the *other side* of the connection and is managed on its own (it
needs an enrollment key and a server to dial), so it is **not** part of
`start-all.sh`.

## Behaviour

- **First run bootstraps deps**: server/broker create their `.venv`, the server
  also installs Playwright Chromium and runs DB migrations, the client runs
  `npm install`. Later runs skip all of that.
- **Detached**: each service is `nohup`-ed; pid → `.run/<svc>.pid`,
  combined stdout+stderr → `.run/<svc>.log` (follow with `tail -f`).
- **Idempotent start**: refuses to double-start if the pid is alive or the port
  is already taken.
- **Robust stop**: SIGTERM the recorded pid (→ SIGKILL after a grace period),
  then free the service's port as a fallback (catches reload/Vite children).
- Runtime state lives in `scripts/.run/` (gitignored).

## Broker pairing

The broker needs a shared secret (`AAKAAR_BROKER_TOKEN`) that the **server must
also use**. `start-broker.sh` generates one if you don't supply it and saves it
to `.run/broker.token`. Wire the server to the broker either way:

```bash
# easiest — server reads .run/broker.token automatically:
AAKAAR_USE_LOCAL_BROKER=1 ./start-server.sh

# or set it explicitly (e.g. in aakaar/.env):
#   AAKAAR_BROKER_URL=ws://127.0.0.1:9300
#   AAKAAR_BROKER_TOKEN=<token from .run/broker.token>
```

`start-all.sh` enables `AAKAAR_USE_LOCAL_BROKER=1` by default.

## Common env knobs

| Variable | Applies to | Default |
|----------|-----------|---------|
| `AAKAAR_API_HOST` / `AAKAAR_API_PORT` | server | `127.0.0.1` / `8000` |
| `AAKAAR_RELOAD=0` | server (disable `--reload`) | `1` |
| `AAKAAR_WEB_HOST` / `AAKAAR_WEB_PORT` | client | vite default / `5173` |
| `AAKAAR_BROKER_HOST` / `AAKAAR_BROKER_PORT` | broker | `127.0.0.1` / `9300` |
| `AAKAAR_BROKER_TOKEN` | broker + server | generated & persisted |
| `AAKAAR_AGENT_SERVER` | agent (server/broker base URL) | `ws://127.0.0.1:8000` |
| `AAKAAR_AGENT_KEY` | agent (**required** enrollment key) | — |
| `AAKAAR_AGENT_EXTRAS` | agent (pip extras, e.g. `gui,record`) | none |
| `AAKAAR_PYTHON` | venv bootstrap | `python3` |
| `AAKAAR_WAIT` | port-up wait (s) | `30` |

## Remote agent (this machine driven by a server elsewhere)

The agent runs on the workstation you want Aakaar to automate and **dials out**
to a server/broker on another machine — it has no inbound port. Typical split:
the **server + client + broker run on one host**, the **agent runs here**.

1. On the server host, enroll an agent (web console → Agents, or
   `POST /agents/enroll`) to get an `<id>.<secret>` key.
2. On this machine, point the agent at the server and start it:

   ```bash
   AAKAAR_AGENT_SERVER=ws://SERVER-HOST:8000 \
   AAKAAR_AGENT_KEY=<id>.<secret> \
   scripts/start-agent.sh
   # …or go through the broker:  AAKAAR_AGENT_SERVER=ws://BROKER-HOST:9300
   # …stop with:                 scripts/stop-agent.sh
   ```

   Tip: drop `AAKAAR_AGENT_SERVER` / `AAKAAR_AGENT_KEY` into `aakaar-agent/.env`
   and just run `scripts/start-agent.sh`. For desktop/RPA capabilities add
   `AAKAAR_AGENT_EXTRAS=gui,record` on the first run (installs the extra deps).

A wrong key or unreachable server shows up in `.run/agent.log` (the agent keeps
retrying), not as a start-time failure — watch it with `tail -f .run/agent.log`.
