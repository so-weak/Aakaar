#!/usr/bin/env bash
# Stop the Aakaar rendezvous broker started by scripts/mac/start-broker.sh.
# SIGTERM the recorded pid (escalate to SIGKILL), then free :9300 as a fallback.
# The persisted scripts/.run/broker.token is left in place so a later restart
# keeps the same secret (and the server stays paired).
set -euo pipefail
# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

stop_service broker "${AAKAAR_BROKER_PORT:-9300}"
