#!/usr/bin/env bash
# Stop the Aakaar API server started by scripts/start-server.sh.
# SIGTERM the recorded pid (escalate to SIGKILL), then free :8000 as a fallback
# to catch any uvicorn --reload child processes.
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

stop_service server "${AAKAAR_API_PORT:-8000}"
