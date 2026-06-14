# 04 — Agent fleet degradation

Remote agents (`aakaar-agent`) dial **out** to the API's `/ws/agents`
endpoint (directly, or via the optional rendezvous broker — see
[05-broker-outage](05-broker-outage.md)), authenticate with a per-agent key
(`<agent_id>.<secret>`; the server stores only a bcrypt hash), send a `hello`
frame announcing OS / GUI session / capabilities, and are then targetable by
DAG nodes with a `target` selector (alias, `pool:<name>`, `os:<name>`).

## Symptoms

- Runs fail with placement errors: `no online agent matches target '...'
  for this tenant` (raised from `aakaar/workers/remote/registry.py`).
- The web UI Agents page shows agents offline; `GET /agents` (tenant admin)
  returns `"online": false` and a stale `last_seen`.
- `POST /placement/check` (body: the DAG) reports issues and
  `online_agents: 0`.
- API log shows a churn of `agent online tenant=... alias=...` /
  disconnect lines — a reconnect storm.

## Diagnosis

### 1. Is it one agent or the fleet?

```bash
TOKEN=...   # tenant-admin bearer token
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/agents | \
  python3 -c 'import json,sys; [print(a["alias"], a["status"], a["online"], a["last_seen"]) for a in json.load(sys.stdin)]'
```

- **Whole fleet offline** → server side: API down/restarted, listener
  disabled, or the network path (firewall, NAT, broker) broke.
- **One agent offline** → workstation side: machine asleep/rebooted, agent
  process dead, agent key revoked.

### 2. Server-side checks

- `AAKAAR_REMOTE_EXEC_ENABLED` must not be `false` — when disabled the API
  closes every agent socket immediately with code **4403**.
- The API must be reachable from the workstations: the dev default binds
  `0.0.0.0:8000` (`dev.sh`), but `AAKAAR_API_HOST=127.0.0.1` or a host
  firewall silently strands LAN agents.
- An API **restart drops every live agent socket** (the registry is
  in-process). Agents reconnect on their own with exponential backoff —
  base 1s doubling to a 60s cap with 0.5–1.0 jitter, counter reset after 30s
  of stable uptime — so the fleet reappears within ~a minute of the API
  coming back. No action needed beyond waiting.

### 3. Workstation-side checks

On the workstation:

```bash
# is the process up? (however it is supervised on that host)
pgrep -fl aakaar-agent

# run it in the foreground with debug logs to see the failure directly:
AAKAAR_AGENT_LOG_LEVEL=DEBUG aakaar-agent \
  --server wss://aakaar.example.com:8000 --key "<agent_id>.<secret>"
```

WebSocket close codes the agent will log tell you exactly why:

| Close code | Meaning | Fix |
|------------|---------|-----|
| 4401 | key missing/malformed/wrong, or the agent was **revoked** (row deleted) | re-enroll: `POST /agents/enroll` → new one-time key |
| 4403 | `AAKAAR_REMOTE_EXEC_ENABLED=false` on the API | re-enable, restart API |
| 4400 | malformed `hello` frame | version skew — update the agent package |
| 1013 | broker at `AAKAAR_BROKER_MAX_SESSIONS` capacity | see [05-broker-outage](05-broker-outage.md) |

Half-dead TCP (workstation slept, NAT entry expired): the connection is
detected dead by the websocket keepalive (ping every 20s, 10s timeout) and
the agent redials. In-flight tasks on the agent keep running across a
disconnect; completed results are **re-delivered** after reconnect, never
re-executed.

### 4. Reconnect storms

A storm (many agents redialing in tight loops) is almost always one of:

- the API accepting TCP but failing auth for everyone — e.g. the agents
  table was restored from an old backup so key hashes don't match current
  keys → re-enroll the fleet;
- a proxy/load balancer in front of `/ws/agents` killing idle WebSockets
  (timeout < 20s keepalive interval) → raise the proxy's idle timeout;
- a crash-looping API → fix the API first ([06-high-error-rate](06-high-error-rate.md)).

The agent backoff is jittered specifically so a mass reconnect (e.g. after an
API restart) does not synchronize; if you observe synchronized hammering, the
agents are likely an old build — update them.

## Key revocation (suspected compromise)

A stolen enrollment key allows connecting *as that agent* and receiving any
task placed on it (including grant-resolved secrets in task frames).

```bash
# 1. Revoke — deletes the row AND drops the live connection immediately:
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/agents/<agent_id>     # 204

# 2. Confirm it is gone / cannot reconnect (agent now gets close code 4401).
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/agents

# 3. Re-enroll the legitimate workstation; the key is shown exactly once:
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"alias": "workstation-1", "pools": ["kiosk"]}' \
  http://localhost:8000/agents/enroll
```

Both actions are audited (`agent.revoke`, `agent.enroll` in `GET /audit`).
After a compromise, also rotate any tenant secrets that were dispatched to
that agent while it may have been impersonated (grant updates via
`PATCH /admin/grants/{id}` require the full secret set).

## Verification

- `GET /agents` shows the agent `"online": true` with a fresh `last_seen`.
- `POST /placement/check` with the affected DAG returns `"issues": []`.
- Re-run the failed workflow: `POST /runs/{run_id}/rerun`.
