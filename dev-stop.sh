#!/usr/bin/env bash
# Stop the services started by ./dev.sh — the Aakaar backend and frontend —
# even if their Terminal tabs are gone.
#
# 8000 — aakaar API (uvicorn)
# 5173 — aakaar-web (Vite)
#
# Tries SIGTERM first, escalates to SIGKILL if anything is still listening.
set -u

PORTS=(8000 5173)

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

echo "Stopping Aakaar dev services..."
for p in "${PORTS[@]}"; do
  free_port "$p"
done
echo "Done."
