#!/usr/bin/env bash
# Start the Aakaar web console (aakaar-web, Vite dev server) in the background
# on :5173. First run installs node_modules. Stop it with scripts/stop-client.sh.
#
# Env knobs:
#   AAKAAR_WEB_HOST=0.0.0.0   expose Vite on the LAN   [vite default: localhost]
#   AAKAAR_WEB_PORT=5173      listen port              [5173]
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

WEB_DIR="$ROOT/aakaar-web"
PORT="${AAKAAR_WEB_PORT:-5173}"

require_cmd npm "npm not found on PATH. Install Node.js (which provides npm) to run the web client."

if [ ! -d "$WEB_DIR/node_modules" ]; then
  log_info "installing web deps (first run) ..."
  ( cd "$WEB_DIR" && npm install )
fi

cd "$WEB_DIR"

# Pass host/port through to Vite. `npm run dev -- <args>` forwards to vite.
VITE_ARGS=(run dev --)
[ -n "${AAKAAR_WEB_HOST:-}" ] && VITE_ARGS+=(--host "$AAKAAR_WEB_HOST")
VITE_ARGS+=(--port "$PORT" --strictPort)

supervise_start client "$PORT" -- npm "${VITE_ARGS[@]}"

log_info "Web:    http://localhost:$PORT"
