#!/usr/bin/env bash
# Retained only as an explicit compatibility tombstone.
set -euo pipefail

cat >&2 <<'EOF'
sync_forecaster_source.sh is disabled.

A forecaster-only Git refresh can make the deployed forecaster tree differ
from the trading tree and its build_info.json revision. Use sync_to_box.sh
from a clean main checkout so both source trees, provenance, dependencies,
and systemd units are deployed as one verified revision.
EOF
exit 2
