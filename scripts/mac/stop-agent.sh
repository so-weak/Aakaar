#!/usr/bin/env bash
# Stop the Aakaar remote-execution agent started by scripts/mac/start-agent.sh.
# The agent has NO listening port, so we SIGTERM the recorded pid (escalate to
# SIGKILL), then kill any leftover agent process from this checkout as a fallback.
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

stop_service agent "" "$ROOT/aakaar-agent/.venv/bin/aakaar-agent"
