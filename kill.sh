#!/usr/bin/env bash
# Free the dev ports the app uses, even if their Terminal windows are gone, and
# stop any local remote-execution agent.
#
# 8000 — aakaar API (uvicorn)
# 8001 — admin-app API (uvicorn)
# 5173 — aakaar-web (Vite)
# 3000 — admin-app (Vite)
# 3001 — nbbl-app  (Vite)
#
# A remote agent dials OUT (no listening port), so it is stopped by process
# name rather than by port.
#
# Tries SIGTERM first, escalates to SIGKILL if anything is still listening.

set -u

PORTS=(8000 8001 5173 3000 3001)

free_port() {
  local port="$1"
  local pids
  pids=$(lsof -ti tcp:"$port" 2>/dev/null || true)
  if [ -z "$pids" ]; then
    echo "  $port: free"
    return
  fi
  echo "  $port: killing pids $pids"
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
  # Give the process a moment to clean up before checking again.
  for _ in 1 2 3 4 5; do
    sleep 0.2
    pids=$(lsof -ti tcp:"$port" 2>/dev/null || true)
    [ -z "$pids" ] && break
  done
  if [ -n "$pids" ]; then
    echo "  $port: still alive, sending SIGKILL"
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
  fi
}

echo "Freeing dev ports..."
for p in "${PORTS[@]}"; do
  free_port "$p"
done

# Stop a local dev remote-execution agent (it has no listening port).
if pgrep -f 'aakaar_agent.main' >/dev/null 2>&1; then
  echo "  agent: stopping local aakaar-agent"
  pkill -f 'aakaar_agent.main' 2>/dev/null || true
else
  echo "  agent: none running"
fi

echo "Done."
