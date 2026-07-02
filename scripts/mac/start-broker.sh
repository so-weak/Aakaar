#!/usr/bin/env bash
# Start the Aakaar rendezvous broker (stateless WebSocket relay) in the
# background on :9300. First run bootstraps its venv. Stop with stop-broker.sh.
#
# The broker REQUIRES a shared secret (AAKAAR_BROKER_TOKEN) and refuses to start
# without one. If you don't provide one, a token is generated and persisted to
# scripts/.run/broker.token so the server can pair with it (see start-server.sh
# / AAKAAR_USE_LOCAL_BROKER=1).
#
# Env knobs:
#   AAKAAR_BROKER_TOKEN=...      shared secret (else taken from aakaar/.env, the
#                               persisted file, or generated)
#   AAKAAR_BROKER_HOST=0.0.0.0   bind address (0.0.0.0 to accept remote agents) [127.0.0.1]
#   AAKAAR_BROKER_PORT=9300      listen port                                    [9300]
#   AAKAAR_PYTHON=python3.12     interpreter used to build the venv             [python3]
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

AAKAAR_PYTHON="${AAKAAR_PYTHON:-python3}"
BROKER_DIR="$ROOT/aakaar-broker"
HOST="${AAKAAR_BROKER_HOST:-0.0.0.0}"
PORT="${AAKAAR_BROKER_PORT:-9300}"

# --- venv --------------------------------------------------------------------
if [ ! -x "$BROKER_DIR/.venv/bin/python" ]; then
  require_cmd "$AAKAAR_PYTHON" "$AAKAAR_PYTHON not found on PATH. Install Python 3.11+ or set AAKAAR_PYTHON."
  log_info "bootstrapping broker venv (first run) ..."
  (
    cd "$BROKER_DIR"
    "$AAKAAR_PYTHON" -m venv .venv
    .venv/bin/python -m pip install --upgrade pip wheel >/dev/null
    .venv/bin/python -m pip install -e .
  )
fi

# --- shared secret -----------------------------------------------------------
TOKEN="$(resolve_broker_token)"
export AAKAAR_BROKER_TOKEN="$TOKEN"
export AAKAAR_BROKER_HOST="$HOST"
export AAKAAR_BROKER_PORT="$PORT"

cd "$BROKER_DIR"
supervise_start broker "$PORT" -- .venv/bin/aakaar-broker

log_info "Broker: ws://$HOST:$PORT   (master: /ws/master, agents: /ws/agents)"
log_info "Token saved to $RUN_DIR/broker.token"
log_info "Point the server at it with either:"
log_info "  AAKAAR_USE_LOCAL_BROKER=1 scripts/mac/start-server.sh"
log_info "  — or set in aakaar/.env:  AAKAAR_BROKER_URL=ws://$HOST:$PORT  and  AAKAAR_BROKER_TOKEN=<token>"
