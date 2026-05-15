#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "${AAKAR_DATA_DIR:-$ROOT/aakar/data}"

# Pick the Python used to bootstrap any missing venvs. Override with
# `AAKAR_PYTHON=python3.13 ./start.sh` if you need a specific version.
AAKAR_PYTHON="${AAKAR_PYTHON:-python3}"

# Bootstrap a virtualenv at <dir>/.venv if it doesn't exist yet.
# `install_cmd` runs after the venv is created with the new bin/ on PATH;
# typical values are `pip install -e .` or `pip install -r requirements.txt`.
#
# Idempotent: if .venv/bin/python already exists we leave it alone — first
# run pays the dependency-install cost, subsequent runs are no-ops.
ensure_venv() {
  local dir="$1"
  local install_cmd="$2"
  local label="$3"

  if [[ -x "$dir/.venv/bin/python" ]]; then
    return 0
  fi

  if ! command -v "$AAKAR_PYTHON" >/dev/null 2>&1; then
    echo "ERROR: $AAKAR_PYTHON not found on PATH. Install Python 3.12+ or" >&2
    echo "       set AAKAR_PYTHON to the interpreter you want to use." >&2
    return 1
  fi

  echo "Bootstrapping $label venv at $dir/.venv (first-time setup)..."
  (
    cd "$dir"
    "$AAKAR_PYTHON" -m venv .venv
    .venv/bin/python -m pip install --upgrade pip wheel >/dev/null
    # shellcheck disable=SC2086  # install_cmd is intentionally word-split
    .venv/bin/python -m pip install $install_cmd
  )
}

ensure_venv "$ROOT/aakar"             "-e ."                  "aakar"
ensure_venv "$ROOT/admin-app/server"  "-r requirements.txt"   "admin-app/server"

# Playwright's Chromium browser is a separate download (not a pip package).
# We mark the install with a sentinel file inside the venv so reruns are cheap.
if [[ ! -f "$ROOT/aakar/.venv/.playwright-chromium-installed" ]]; then
  echo "Installing Playwright Chromium (first-time setup, ~150 MB)..."
  (cd "$ROOT/aakar" && .venv/bin/python -m playwright install chromium)
  touch "$ROOT/aakar/.venv/.playwright-chromium-installed"
fi

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

# Single-threaded numerical libs in the API process. We use BGE embeddings
# for capability search; their multi-thread machinery (joblib/loky/OpenMP)
# leaves leaked-semaphore warnings on shutdown without buying us speed for
# our tiny per-request workloads. These vars must be set *before* the
# Python process starts — the libs read them at import time.
AAKAR_PY_ENV='OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false LOKY_MAX_CPU_COUNT=1'

open_in_terminal "cd '$ROOT/aakar' && env $AAKAR_PY_ENV .venv/bin/uvicorn aakar.api.main:app --reload --reload-dir aakar --host 127.0.0.1 --port 8000"
open_in_terminal "cd '$ROOT/admin-app/server' && .venv/bin/uvicorn main:app --reload --host 127.0.0.1 --port 8001"
open_in_terminal "cd '$ROOT/aakar-web' && npm run dev"
open_in_terminal "cd '$ROOT/admin-app' && npm run dev"
open_in_terminal "cd '$ROOT/nbbl-app' && npm run dev"

cat <<EOF
Launched each service in its own Terminal window:
  aakar api:        http://localhost:8000
  admin-app api:    http://localhost:8001
  aakar-web:        http://localhost:5173
  admin-app:        http://localhost:3000
  nbbl-app:         http://localhost:3001

Stop a service with Ctrl+C in its window, or close the window (Cmd+W).
EOF
