#!/usr/bin/env bash
# Start the Aakaar API server (FastAPI/uvicorn) in the background on :8000.
# First run bootstraps the venv, installs Playwright Chromium, and runs DB
# migrations; later runs are fast. Stop it with scripts/stop-server.sh.
#
# Env knobs:
#   AAKAAR_API_HOST=0.0.0.0     bind address                       [127.0.0.1]
#   AAKAAR_API_PORT=8000        listen port                        [8000]
#   AAKAAR_RELOAD=0             disable uvicorn --reload            [1]
#   AAKAAR_USE_LOCAL_BROKER=1   pair with the broker started by start-broker.sh
#   AAKAAR_PYTHON=python3.12    interpreter used to build the venv  [python3]
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

AAKAAR_PYTHON="${AAKAAR_PYTHON:-python3}"
APP_DIR="$ROOT/aakaar"
HOST="${AAKAAR_API_HOST:-0.0.0.0}"
PORT="${AAKAAR_API_PORT:-8000}"

mkdir -p "${AAKAAR_DATA_DIR:-$APP_DIR/data}"

# --- venv (server + shared capability library, both editable) ----------------
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  require_cmd "$AAKAAR_PYTHON" "$AAKAAR_PYTHON not found on PATH. Install Python 3.12+ or set AAKAAR_PYTHON."
  log_info "bootstrapping server venv (first run) ..."
  (
    cd "$APP_DIR"
    "$AAKAAR_PYTHON" -m venv .venv
    .venv/bin/python -m pip install --upgrade pip wheel >/dev/null
    .venv/bin/python -m pip install -e . -e ../aakaar-capabilities
  )
fi

# --- Playwright Chromium (one-time, ~150 MB; sentinel keeps reruns cheap) -----
if [ ! -f "$APP_DIR/.venv/.playwright-chromium-installed" ]; then
  log_info "installing Playwright Chromium (first run, ~150 MB) ..."
  ( cd "$APP_DIR" && .venv/bin/python -m playwright install chromium )
  touch "$APP_DIR/.venv/.playwright-chromium-installed"
fi

# --- DB migrations -----------------------------------------------------------
log_info "running migrations ..."
( cd "$APP_DIR" && .venv/bin/alembic upgrade head )

# --- env ---------------------------------------------------------------------
cd "$APP_DIR"
# Your AAKAAR_JWT_SECRET / OPENAI_API_KEY / broker config live in aakaar/.env.
if [ -f .env ]; then set -a; . ./.env; set +a; fi
# Generate a throwaway JWT secret only if one isn't set anywhere.
export AAKAAR_JWT_SECRET="${AAKAAR_JWT_SECRET:-$("$AAKAAR_PYTHON" -c 'import secrets; print(secrets.token_urlsafe(48))')}"
# Single-threaded numeric libs: the BGE embedding stack's threading leaves
# leaked-semaphore warnings on shutdown without buying speed here. Must be set
# before the process starts (libs read them at import).
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
       TOKENIZERS_PARALLELISM=false LOKY_MAX_CPU_COUNT=1

# Optional: auto-pair with the locally started broker (token from start-broker.sh).
if [ "${AAKAAR_USE_LOCAL_BROKER:-0}" = "1" ] && [ -z "${AAKAAR_BROKER_URL:-}" ]; then
  tok="$(cat "$RUN_DIR/broker.token" 2>/dev/null || true)"
  if [ -n "$tok" ]; then
    export AAKAAR_BROKER_URL="ws://${AAKAAR_BROKER_HOST:-127.0.0.1}:${AAKAAR_BROKER_PORT:-9300}"
    export AAKAAR_BROKER_TOKEN="$tok"
    log_info "pairing with local broker at $AAKAAR_BROKER_URL"
  else
    log_warn "AAKAAR_USE_LOCAL_BROKER=1 but no token at $RUN_DIR/broker.token — start the broker first."
  fi
fi

RELOAD_ARGS=()
if [ "${AAKAAR_RELOAD:-1}" = "1" ]; then
  RELOAD_ARGS=(--reload --reload-dir aakaar)
fi

supervise_start server "$PORT" -- \
  .venv/bin/uvicorn aakaar.api.main:app "${RELOAD_ARGS[@]}" --host "$HOST" --port "$PORT"

log_info "API:    http://$HOST:$PORT   (health: http://$HOST:$PORT/healthz)"
