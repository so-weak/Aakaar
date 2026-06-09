#!/usr/bin/env bash
# Start ONLY the Aakaar backend (API) and frontend (web), each in a new
# Terminal tab. Bootstraps the backend venv and the frontend node_modules
# on first run; subsequent runs are fast no-ops.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AAKAAR_PYTHON="${AAKAAR_PYTHON:-python3}"

mkdir -p "${AAKAAR_DATA_DIR:-$ROOT/aakaar/data}"

# --- backend venv (create if missing) --------------------------------------
if [[ ! -x "$ROOT/aakaar/.venv/bin/python" ]]; then
  echo "Bootstrapping backend venv (first-time setup)..."
  (
    cd "$ROOT/aakaar"
    "$AAKAAR_PYTHON" -m venv .venv
    .venv/bin/python -m pip install --upgrade pip wheel >/dev/null
    # server + shared capability library, both editable
    .venv/bin/python -m pip install -e . -e ../aakaar-capabilities
    # Playwright Chromium for browser capabilities (~150 MB, one-time)
    .venv/bin/python -m playwright install chromium
  )
fi

# --- frontend deps (install if missing) ------------------------------------
if [[ ! -d "$ROOT/aakaar-web/node_modules" ]]; then
  echo "Installing frontend deps (first-time setup)..."
  (cd "$ROOT/aakaar-web" && npm install)
fi

# --- DB migrations ----------------------------------------------------------
echo "Running migrations..."
(cd "$ROOT/aakaar" && .venv/bin/alembic upgrade head)

# Open a command in a NEW Terminal tab. Opening a *tab* uses System Events,
# which needs Accessibility permission for the app you launch from (Terminal /
# iTerm / VS Code: System Settings > Privacy & Security > Accessibility).
# If that's not granted, we fall back to a new *window*, which needs no
# permission — so this always works.
open_in_tab() {
  local cmd="$1"
  local escaped="${cmd//\\/\\\\}"
  escaped="${escaped//\"/\\\"}"
  if osascript \
      -e 'tell application "Terminal" to activate' \
      -e 'tell application "System Events" to keystroke "t" using command down' \
      -e 'delay 0.5' \
      -e "tell application \"Terminal\" to do script \"$escaped\" in front window" >/dev/null 2>&1; then
    return 0
  fi
  # Fallback: new window (no Accessibility permission required).
  osascript \
    -e "tell application \"Terminal\" to do script \"$escaped\"" \
    -e 'tell application "Terminal" to activate' >/dev/null
}

# Single-threaded numerical libs in the API process (matches start.sh): avoids
# leaked-semaphore warnings from the embedding stack on shutdown.
PY_ENV='OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false LOKY_MAX_CPU_COUNT=1'

# Backend: source aakaar/.env if present (your AAKAAR_JWT_SECRET / OPENAI_API_KEY
# live there); generate a throwaway JWT secret only if one isn't set anywhere.
BACKEND_CMD="cd '$ROOT/aakaar' && set -a; [ -f .env ] && . ./.env; set +a; export AAKAAR_JWT_SECRET=\${AAKAAR_JWT_SECRET:-\$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')} && env $PY_ENV .venv/bin/uvicorn aakaar.api.main:app --reload --reload-dir aakaar --host 127.0.0.1 --port 8000"

FRONTEND_CMD="cd '$ROOT/aakaar-web' && npm run dev"

open_in_tab "$BACKEND_CMD"
open_in_tab "$FRONTEND_CMD"

cat <<EOF
Launched in new Terminal tabs:
  Aakaar API:     http://localhost:8000
  Aakaar web UI:  http://localhost:5173

Stop a service with Ctrl+C in its tab.
EOF
