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
