#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "${AAKAAR_DATA_DIR:-$ROOT/aakaar/data}"

# Pick the Python used to bootstrap any missing venvs. Override with
# `AAKAAR_PYTHON=python3.13 ./start.sh` if you need a specific version.
AAKAAR_PYTHON="${AAKAAR_PYTHON:-python3}"

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

  if ! command -v "$AAKAAR_PYTHON" >/dev/null 2>&1; then
    echo "ERROR: $AAKAAR_PYTHON not found on PATH. Install Python 3.12+ or" >&2
    echo "       set AAKAAR_PYTHON to the interpreter you want to use." >&2
    return 1
  fi

  echo "Bootstrapping $label venv at $dir/.venv (first-time setup)..."
  (
    cd "$dir"
    "$AAKAAR_PYTHON" -m venv .venv
    .venv/bin/python -m pip install --upgrade pip wheel >/dev/null
    # shellcheck disable=SC2086  # install_cmd is intentionally word-split
    .venv/bin/python -m pip install $install_cmd
  )
}

# The server also installs the shared capability library (aakaar-capabilities)
# editable, so write-once capabilities run on the server or a remote agent from
# a single source. Relative path resolves against the venv dir we cd into.
ensure_venv "$ROOT/aakaar"             "-e . -e ../aakaar-capabilities"  "aakaar"
ensure_venv "$ROOT/admin-app/server"  "-r requirements.txt"             "admin-app/server"

# Playwright's Chromium browser is a separate download (not a pip package).
# We mark the install with a sentinel file inside the venv so reruns are cheap.
if [[ ! -f "$ROOT/aakaar/.venv/.playwright-chromium-installed" ]]; then
  echo "Installing Playwright Chromium (first-time setup, ~150 MB)..."
  (cd "$ROOT/aakaar" && .venv/bin/python -m playwright install chromium)
  touch "$ROOT/aakaar/.venv/.playwright-chromium-installed"
fi

echo "Running migrations..."
(cd "$ROOT/aakaar" && .venv/bin/alembic upgrade head)

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
AAKAAR_PY_ENV='OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false LOKY_MAX_CPU_COUNT=1'

open_in_terminal "cd '$ROOT/aakaar' && env $AAKAAR_PY_ENV .venv/bin/uvicorn aakaar.api.main:app --reload --reload-dir aakaar --host 127.0.0.1 --port 8000"
open_in_terminal "cd '$ROOT/admin-app/server' && .venv/bin/uvicorn main:app --reload --host 127.0.0.1 --port 8001"
open_in_terminal "cd '$ROOT/aakaar-web' && npm run dev"
open_in_terminal "cd '$ROOT/admin-app' && npm run dev"
open_in_terminal "cd '$ROOT/nbbl-app' && npm run dev"

# Optional: launch a LOCAL remote-execution agent for development. Agents are
# normally installed on the workstations they automate, NOT the server host, so
# this is off by default. To run one here, first enroll an agent (Agents page or
# POST /agents/enroll) to get an enrollment key, then:
#   AAKAAR_START_AGENT=1 AAKAAR_AGENT_KEY='<id>.<secret>' ./start.sh
AGENT_LINE="(none — set AAKAAR_START_AGENT=1 + AAKAAR_AGENT_KEY to run a local dev agent)"
if [[ "${AAKAAR_START_AGENT:-}" == "1" ]]; then
  if [[ -z "${AAKAAR_AGENT_KEY:-}" ]]; then
    echo "AAKAAR_START_AGENT=1 but AAKAAR_AGENT_KEY is empty; not starting an agent." >&2
  else
    # The agent runs the same shared capabilities as the server, so it installs
    # aakaar-capabilities editable too (single source of truth for shell_exec,
    # system_info, json_extract, …).
    ensure_venv "$ROOT/aakaar-agent" "-e . -e ../aakaar-capabilities" "aakaar-agent"
    open_in_terminal "cd '$ROOT/aakaar-agent' && AAKAAR_AGENT_SERVER='${AAKAAR_AGENT_SERVER:-ws://127.0.0.1:8000}' AAKAAR_AGENT_KEY='$AAKAAR_AGENT_KEY' .venv/bin/aakaar-agent"
    AGENT_LINE="connecting to ${AAKAAR_AGENT_SERVER:-ws://127.0.0.1:8000}/ws/agents"
  fi
fi

cat <<EOF
Launched each service in its own Terminal window:
  aakaar api:        http://localhost:8000
  admin-app api:    http://localhost:8001
  aakaar-web:        http://localhost:5173
  admin-app:        http://localhost:3000
  nbbl-app:         http://localhost:3001
  remote agent:      $AGENT_LINE

Stop a service with Ctrl+C in its window, or close the window (Cmd+W).
Stop everything (incl. a local agent) with ./kill.sh.
EOF
