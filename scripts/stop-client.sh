#!/usr/bin/env bash
# Stop the Aakaar web console started by scripts/start-client.sh.
# SIGTERM the recorded pid (escalate to SIGKILL), then free :5173 as a fallback
# to catch the Vite/esbuild child processes npm spawns.
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

stop_service client "${AAKAAR_WEB_PORT:-5173}"
