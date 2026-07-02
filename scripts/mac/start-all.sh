#!/usr/bin/env bash
# Convenience: start the whole local stack in dependency order —
#   broker (so the server can pair with it) -> server -> client.
# Each service is independent; this just calls the per-service scripts.
# Pass AAKAAR_USE_LOCAL_BROKER=0 to start the server WITHOUT broker pairing.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$DIR/start-broker.sh"
AAKAAR_USE_LOCAL_BROKER="${AAKAAR_USE_LOCAL_BROKER:-1}" "$DIR/start-server.sh"
"$DIR/start-client.sh"

echo
echo "Stack up. Stop everything with scripts/mac/stop-all.sh"
