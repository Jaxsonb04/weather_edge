#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FORECASTER_DIR="${SFO_FORECASTER_ROOT:-/opt/weatheredge/forecaster}"
TRADING_DIR="${SFO_TRADING_ROOT:-/opt/weatheredge/trading}"
PYTHON_BIN="${SFO_TRADING_PYTHON:-$TRADING_DIR/.venv/bin/python}"
MANIFEST_OUTPUT_PATH="${SFO_PUBLICATION_MANIFEST_PATH:-$FORECASTER_DIR/publication_manifest.json}"
ARTIFACT_LOCK="${SFO_ARTIFACT_GENERATION_LOCK:-/opt/weatheredge/.locks/artifact-generation.lock}"
LOCK_WAIT_SECONDS="${SFO_ARTIFACT_LOCK_WAIT_SECONDS:-900}"

case "$MODE" in
  operational)
    BUILDER="$SCRIPT_DIR/build_public_trading_signal.sh"
    ;;
  strategy)
    BUILDER="$SCRIPT_DIR/build_strategy_research.sh"
    ;;
  *)
    echo "usage: $0 operational|strategy" >&2
    exit 2
    ;;
esac

mkdir -p "$(dirname "$ARTIFACT_LOCK")"
exec 7>"$ARTIFACT_LOCK"
if ! flock -w "$LOCK_WAIT_SECONDS" 7; then
  echo "timed out waiting for artifact generation lock: $ARTIFACT_LOCK" >&2
  exit 1
fi

# Keep one lock across generation, manifest validation, and the publisher's
# copy. Child scripts use this marker instead of trying to reacquire it.
export SFO_ARTIFACT_LOCK_HELD=1
/bin/bash "$BUILDER"
if [[ "$MODE" == "strategy" ]]; then
  "$PYTHON_BIN" -m sfo_kalshi_quant.publication build \
    --artifact-root "$FORECASTER_DIR" \
    --output "$MANIFEST_OUTPUT_PATH" >/dev/null
  export SFO_REQUIRE_STRATEGY_ARTIFACT=1
fi
/bin/bash "$SCRIPT_DIR/publish_forecaster_pages.sh"
