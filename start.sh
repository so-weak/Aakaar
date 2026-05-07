#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "${AAKAR_DATA_DIR:-$ROOT/aakar/data}"

echo "Running migrations..."
(cd "$ROOT/aakar" && .venv/bin/alembic upgrade head)

open_in_terminal() {
  local cmd="$1"
  local escaped="${cmd//\\/\\\\}"
  escaped="${escaped//\"/\\\"}"
  osascript \
    -e "tell application \"Terminal\" to do script \"$escaped\"" \
    -e 'tell application "Terminal" to activate' >/dev/null
}

open_in_terminal "cd '$ROOT/aakar' && .venv/bin/uvicorn aakar.api.main:app --reload --reload-dir aakar --host 127.0.0.1 --port 8000"
open_in_terminal "cd '$ROOT/aakar-web' && npm run dev"
open_in_terminal "cd '$ROOT/admin-app' && npm run dev"
open_in_terminal "cd '$ROOT/nbbl-app' && npm run dev"

cat <<EOF
Launched each service in its own Terminal window:
  aakar api:  http://localhost:8000
  aakar-web:  http://localhost:5173
  admin-app:  http://localhost:3000
  nbbl-app:   http://localhost:3001

Stop a service with Ctrl+C in its window, or close the window (Cmd+W).
EOF
