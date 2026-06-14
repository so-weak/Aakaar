# Aakaar rendezvous broker (`aakaar-broker`)

A tiny, **stateless** WebSocket relay that lets remote agents and the Aakaar
API find each other **by identity instead of by IP**. Both sides dial *out* to
the broker's one stable address; the broker pairs each agent session onto the
API's master link and relays frames blindly:

```
 workstation A                                          Aakaar API server
 ┌──────────────┐                                       ┌──────────────────┐
 │ aakaar-agent ──ws──▶                          ◀──ws── master link       │
 └──────────────┘       ┌──────────────────────┐        │ (AAKAAR_BROKER_  │
 workstation B          │  aakaar-broker       │        │  URL + TOKEN)    │
 ┌──────────────┐       │  /ws/agents  ◀─mux─▶ │        └──────────────────┘
 │ aakaar-agent ──ws──▶ │  /ws/master          │
 └──────────────┘       └──────────────────────┘
                         one stable address
```

Each agent socket gets a session id; the broker announces it up the master
link (`{"t":"open","sid",…}`) and from then on relays text frames verbatim in
both directions, multiplexed by `sid`. The broker **never parses agent frames
and never verifies agent credentials** — the agent's `x-agent-key` header is
forwarded opaquely and the API performs the authoritative check (against its
DB) before the session does anything, exactly as for a direct connection. The
only secret the broker checks is its own shared `AAKAAR_BROKER_TOKEN` on the
master link, so nobody else can impersonate the API and receive agent sessions.

## Trust model — the broker host is trusted infrastructure

"Verified by the API" does **not** mean the broker can't use the key. The
broker is a fully-trusted component, on par with the API host itself. Run it
only on hardware you control. Concretely, a malicious or compromised broker
operator can:

- **Read every agent's enrollment key in cleartext.** The `x-agent-key` header
  physically transits the broker process so it can be copied into the `open`
  envelope. The broker is told never to log it, but a hostile operator simply
  ignores that — and can then replay each captured key against the API's
  `/ws/agents` endpoint to impersonate that agent from anywhere. (This is why
  the broker should sit behind TLS even on a LAN: the same exposure applies to
  anyone sniffing the link, not just the operator.)
- **Forge frames on any session it is currently relaying.** Once the API has
  authenticated a session, every `data` frame the API receives for that `sid`
  arrives solely from the broker. The broker can therefore inject arbitrary
  `{"type":"result"}` frames (resolving a dispatched task with attacker-chosen
  output) or `{"type":"event"}` frames into a live, already-authenticated
  session. It cannot, however, claim a frame belongs to a *different* tenant:
  the API pins each session — including its recorded events — to the
  `tenant_id` of the DB-verified key, so a compromised broker is confined to
  impersonating/forging the agents whose sessions it is already relaying and
  cannot cross tenant boundaries.

What the broker token buys you is only that **outsiders** can't connect a rogue
master link and harvest sessions; it does nothing to constrain the broker host
itself. If that trust is too broad for your environment, run the broker on the
same trust boundary as the API, or pursue a per-agent pre-shared broker token /
mutual-TLS scheme so the agent's enrollment key never has to transit the broker
in the clear (a planned follow-up, not yet implemented).

## When you need it (and when you don't)

- **Direct dial (default, no broker):** the agent can reach the API host at a
  stable name/IP (`AAKAAR_AGENT_SERVER=wss://aakaar.example.com:8000`). Keep
  doing that — the broker adds a hop for no benefit.
- **Broker:** *neither* side has an address the other can reach — both the API
  box and the workstations sit on DHCP / changing networks / behind NAT, or on
  different networks entirely. Run the broker on the one machine with a stable
  address (a small VM is plenty); everything else can float. Direct
  connections keep working alongside — the broker is purely additive.

## Run it

```bash
pip install ./aakaar-broker         # only dependency: websockets

export AAKAAR_BROKER_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
aakaar-broker                       # listens on ws://127.0.0.1:9300
```

| Env var | Default | Meaning |
|---|---|---|
| `AAKAAR_BROKER_TOKEN` | **required, no default** — the process refuses to start without it | shared secret the API presents on the master link |
| `AAKAAR_BROKER_HOST` | `127.0.0.1` | bind address (set `0.0.0.0` only behind a TLS proxy / firewall) |
| `AAKAAR_BROKER_PORT` | `9300` | bind port |
| `AAKAAR_BROKER_MAX_SESSIONS` | `200` | concurrent agent sessions; extras are refused with close code 1013 |
| `AAKAAR_BROKER_HANDSHAKE_TIMEOUT` | `10` (seconds) | an agent session the API hasn't answered by then is dropped (close code 4408) |

Point the **API** at it (both vars, or the API refuses to start):

```bash
AAKAAR_BROKER_URL=wss://broker.example.com \
AAKAAR_BROKER_TOKEN=<same shared secret> \
  uvicorn aakaar.api.main:app ...
```

Point each **agent** at it — no agent code or flag changes, just the server URL:

```bash
AAKAAR_AGENT_SERVER=wss://broker.example.com aakaar-agent --key "<agent_id>.<secret>"
```

## TLS: put it behind a reverse proxy

The broker itself speaks plain `ws://`. Anywhere outside a trusted LAN you
**must** terminate TLS in front of it (the agent key header and all task
traffic transit this link), e.g. nginx:

```nginx
server {
    listen 443 ssl;
    server_name broker.example.com;
    # ssl_certificate / ssl_certificate_key ...
    location / {
        proxy_pass http://127.0.0.1:9300;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header X-Agent-Key $http_x_agent_key;
        proxy_read_timeout 90s;     # > the 20s websocket ping interval
    }
}
```

Then use `wss://broker.example.com` in `AAKAAR_BROKER_URL` and
`AAKAAR_AGENT_SERVER`.

## systemd unit

`/etc/systemd/system/aakaar-broker.service`:

```ini
[Unit]
Description=Aakaar rendezvous broker
After=network-online.target
Wants=network-online.target

[Service]
User=aakaar-broker
EnvironmentFile=/etc/aakaar/broker.env   # AAKAAR_BROKER_TOKEN=... (chmod 600)
ExecStart=/opt/aakaar-broker/.venv/bin/aakaar-broker
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now aakaar-broker
```

## Behavior notes

- **Stateless:** no disk, no DB. If the broker restarts, agents and the API
  simply reconnect (both already retry with backoff) and re-pair.
- **Single master link:** a second connection presenting a valid token
  *replaces* the old one (logged as a warning) — this is how the API resumes
  after a restart while its old TCP connection is still half-open. Live agent
  sessions are closed (code 1012) and reconnect through the new link.
- **No master online:** agents are refused with close code 1013 (try again
  later) and keep retrying.
- **Keepalive:** websocket pings every 20s on every connection; half-dead
  links are torn down by the protocol layer.

## Develop / test

```bash
cd aakaar-broker
pip install -e '.[dev]'
python -m pytest
ruff check aakaar_broker tests
```
