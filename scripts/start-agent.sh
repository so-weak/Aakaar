#!/usr/bin/env bash
# Start the Aakaar remote-execution agent in the background. The agent dials OUT
# to the server (or broker) and has NO listening port — run it on the machine you
# want Aakaar to drive, pointed at a server running elsewhere. Stop it with
# scripts/stop-agent.sh.
#
# REQUIRED (env, or aakaar-agent/.env):
#   AAKAAR_AGENT_KEY=<id>.<secret>   enrollment key from the server's Agents page
#                                    (or POST /agents/enroll)
# Common knobs:
#   AAKAAR_AGENT_SERVER=ws://SERVER-HOST:8000   server base URL  [ws://127.0.0.1:8000]
#                                    (point at the broker's ws://HOST:9300 to relay via the broker)
#   AAKAAR_AGENT_EXTRAS=gui,record   pip extras for desktop/recording caps     [none]
#   AAKAAR_AGENT_LOG_LEVEL=DEBUG     agent log verbosity                        [INFO]
#   AAKAAR_PYTHON=python3.12         interpreter used to build the venv         [python3]
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

AAKAAR_PYTHON="${AAKAAR_PYTHON:-python3}"
AGENT_DIR="$ROOT/aakaar-agent"
AGENT_BIN="$AGENT_DIR/.venv/bin/aakaar-agent"

# Persist server/key in aakaar-agent/.env if you prefer not to pass them inline.
if [ -f "$AGENT_DIR/.env" ]; then set -a; . "$AGENT_DIR/.env"; set +a; fi

SERVER="${AAKAAR_AGENT_SERVER:-ws://127.0.0.1:8000}"
KEY="${AAKAAR_AGENT_KEY:-}"

# Fail fast (before the venv build) with an actionable message.
if [ -z "$KEY" ]; then
  log_err "AAKAAR_AGENT_KEY is not set — the agent needs an enrollment key to connect."
  log_err "Enroll an agent on the server (Agents page or POST /agents/enroll) to get an"
  log_err "'<id>.<secret>' key, then run:"
  log_err "  AAKAAR_AGENT_SERVER=ws://YOUR-SERVER:8000 AAKAAR_AGENT_KEY=<id>.<secret> scripts/start-agent.sh"
  exit 1
fi

case "$SERVER" in
  ws://127.0.0.1:*|ws://localhost:*|wss://127.0.0.1:*|wss://localhost:*)
    log_warn "AAKAAR_AGENT_SERVER=$SERVER (local). The server is usually on ANOTHER machine —"
    log_warn "set AAKAAR_AGENT_SERVER=ws://THAT-HOST:8000 (or the broker's ws://THAT-HOST:9300)."
    ;;
esac

# --- venv (agent + shared capability library, both editable) -----------------
if [ ! -x "$AGENT_DIR/.venv/bin/python" ]; then
  require_cmd "$AAKAAR_PYTHON" "$AAKAAR_PYTHON not found on PATH. Install Python 3.11+ or set AAKAAR_PYTHON."
  log_info "bootstrapping agent venv (first run) ..."
  extras=""
  [ -n "${AAKAAR_AGENT_EXTRAS:-}" ] && extras="[${AAKAAR_AGENT_EXTRAS}]"
  (
    cd "$AGENT_DIR"
    "$AAKAAR_PYTHON" -m venv .venv
    .venv/bin/python -m pip install --upgrade pip wheel >/dev/null
    .venv/bin/python -m pip install -e ".${extras}" -e ../aakaar-capabilities
  )
fi

export AAKAAR_AGENT_SERVER="$SERVER"
export AAKAAR_AGENT_KEY="$KEY"

cd "$AGENT_DIR"
# Portless: supervise_start only verifies the process stays up. The agent
# reconnects on its own, so a wrong server/key surfaces in the log, not here.
# Launched by ABSOLUTE path so stop-agent.sh can match it as a fallback.
supervise_start agent "" -- "$AGENT_BIN"

log_info "Agent:  dialing ${SERVER%/}/ws/agents — follow it with: tail -f $RUN_DIR/agent.log"
