# 05 — Broker outage

Applies only to deployments that run the optional rendezvous broker
(`aakaar-broker/`) because neither the API nor the workstations have a
stable address. If `AAKAAR_BROKER_URL` is unset on the API, you don't run a
broker and this runbook does not apply.

Topology refresher: agents dial the broker's `/ws/agents`; the API dials out
to the broker's `/ws/master` (authenticated by the shared
`AAKAAR_BROKER_TOKEN`); the broker pairs each agent socket onto the master
link and relays frames blindly. It is **stateless** — it verifies no agent
credentials and holds no queue. Agent keys are verified end-to-end by the
API, identically to direct connections. Direct connections keep working
alongside a broker at all times.

## Symptoms

- All *broker-routed* agents offline at once (`GET /agents` → `online: false`),
  while the API itself is healthy (`/healthz` ok, UI fine).
- API log: `broker master link reconnecting in Ns` repeating
  (`aakaar.workers.remote.broker_link`).
- Agents pointed at the broker log connection-refused / DNS errors, or close
  code **1013** (broker at `AAKAAR_BROKER_MAX_SESSIONS` capacity) or **4408**
  (the API's master link never answered the session within
  `AAKAAR_BROKER_HANDSHAKE_TIMEOUT`, default 10s).

## Diagnose: which leg is down?

```
agent --ws--> broker <--ws-- API (master link)
```

1. **Broker process:** on the broker host —
   `pgrep -fl aakaar-broker`; try a raw TCP/WS dial from anywhere:
   `python3 -c "import socket; socket.create_connection(('broker.example.com', 9300), 5); print('tcp ok')"`.
2. **API→broker leg:** API log. A healthy link logs no reconnect lines; a
   broken one retries with backoff forever (the API does not crash when the
   broker is away — relayed agents are simply absent).
   - If the API *refused to start* with
     `AAKAAR_BROKER_URL is set but AAKAAR_BROKER_TOKEN is not` — the token is
     missing in the API env; this is a fail-closed startup check.
   - A token **mismatch** shows as the broker rejecting the master link and
     the API retrying; verify both processes carry the same
     `AAKAAR_BROKER_TOKEN`.
3. **Agent→broker leg:** agent logs on a workstation (see
   [04-agent-fleet-degradation](04-agent-fleet-degradation.md) for codes).
   1013 = capacity, not an outage: raise `AAKAAR_BROKER_MAX_SESSIONS` only
   after confirming the sessions are legitimate.

## Restore the broker

The broker is stateless, so recovery is just restarting it with the same
token and address:

```bash
# on the broker host
export AAKAAR_BROKER_TOKEN='<same value the API holds>'
export AAKAAR_BROKER_HOST=0.0.0.0     # only behind TLS proxy / firewall
export AAKAAR_BROKER_PORT=9300
aakaar-broker
```

It refuses to start without `AAKAAR_BROKER_TOKEN` — deliberately, no default.
On every master-link drop the broker closes its agent sockets; agents and the
API both re-dial with backoff, so the fleet converges within ~a minute of the
broker returning. Nothing to replay, nothing to resume.

## Fallback: direct dial (bypass the broker)

If the broker host is gone for a while and the API *does* have an address the
workstations can reach (LAN, VPN, port-forward), repoint the agents directly:

```bash
# on each workstation — same key, different server URL:
aakaar-agent --server wss://aakaar.example.com:8000 --key "<agent_id>.<secret>"
# (or AAKAAR_AGENT_SERVER / AAKAAR_AGENT_KEY in the agent's environment)
```

No server change is needed: direct `/ws/agents` connections are always
accepted (the broker is purely additive), the same enrollment key works on
both paths, and the dispatcher cannot tell relayed agents from direct ones.
Leave `AAKAAR_BROKER_URL` set on the API — the master link keeps retrying
quietly and relayed agents come back when the broker does.

If the broker is *permanently* retired, unset `AAKAAR_BROKER_URL` (and the
API-side `AAKAAR_BROKER_TOKEN`) and restart the API to stop the retry loop.

## During the outage

- Runs whose nodes target broker-routed agents fail placement with
  `no online agent matches target '...' for this tenant` — they fail fast
  rather than queue. Re-run them after recovery: `POST /runs/{run_id}/rerun`.
- Schedules keep firing; their runs fail the same way. Disable a noisy
  schedule with `PATCH /schedules/{id} {"enabled": false}` if needed.

## Hardening afterthoughts

- Run the broker under a supervisor (systemd `Restart=always`) — it is a
  single small process with no disk state.
- Treat `AAKAAR_BROKER_TOKEN` like a JWT secret: anyone holding it can
  impersonate the API's master link and receive agent sessions (and thus
  task frames containing grant-resolved secrets).
- Keep `AAKAAR_BROKER_HOST=127.0.0.1` unless fronted by TLS/firewall.
