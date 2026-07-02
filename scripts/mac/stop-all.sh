#!/usr/bin/env bash
# Convenience: stop the whole local stack (reverse of start-all.sh).
# Keeps going even if one service was already down.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$DIR/stop-client.sh" || true
"$DIR/stop-server.sh" || true
"$DIR/stop-broker.sh" || true
