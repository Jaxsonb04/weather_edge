#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "DEPRECATED: sync_to_lightsail.sh forwards to sync_to_box.sh; update callers." >&2
exec "$SCRIPT_DIR/sync_to_box.sh" "$@"
