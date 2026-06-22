#!/usr/bin/env bash
# Server-side setup: rebind the broker and API server to ALL interfaces (0.0.0.0)
# so a remote agent on another machine can dial in over the LAN.
#
# Run this ON THE SERVER MACHINE (the one running the broker + server).
# By default they bind 127.0.0.1 (loopback only) and are unreachable from other
# hosts; this restarts them on 0.0.0.0 instead.
#
# Usage:
#   scripts/server-side-setup.sh
set -euo pipefail
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$SCRIPTS_DIR/stop-server.sh" && "$SCRIPTS_DIR/stop-broker.sh"
AAKAAR_BROKER_HOST=0.0.0.0 "$SCRIPTS_DIR/start-broker.sh"
AAKAAR_API_HOST=0.0.0.0 AAKAAR_USE_LOCAL_BROKER=1 "$SCRIPTS_DIR/start-server.sh"
