#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FORECASTER_DIR="${SFO_FORECASTER_ROOT:-/opt/weatheredge/forecaster}"
TRADING_DIR="${SFO_TRADING_ROOT:-/opt/weatheredge/trading}"
PYTHON_BIN="${SFO_TRADING_PYTHON:-$TRADING_DIR/.venv/bin/python}"
MANIFEST_OUTPUT_PATH="${SFO_PUBLICATION_MANIFEST_PATH:-$FORECASTER_DIR/publication_manifest.json}"
STRATEGY_OUTPUT_PATH="${SFO_STRATEGY_RESEARCH_PATH:-$FORECASTER_DIR/strategy_research.json}"
ARTIFACT_LOCK="${SFO_ARTIFACT_GENERATION_LOCK:-/opt/weatheredge/.locks/artifact-generation.lock}"

case "$MODE" in
  operational)
    BUILDER="$SCRIPT_DIR/build_public_trading_signal.sh"
    LOCK_WAIT_SECONDS="${SFO_OPERATIONAL_ARTIFACT_LOCK_WAIT_SECONDS:-60}"
    ;;
  strategy)
    BUILDER="$SCRIPT_DIR/build_strategy_research.sh"
    LOCK_WAIT_SECONDS="${SFO_STRATEGY_ARTIFACT_LOCK_WAIT_SECONDS:-30}"
    # This publication entry point is intentionally bounded even when an
    # EnvironmentFile sets the flag to 0. Full analysis has a separate,
    # deploy-time maintenance helper and never runs on the recurring timer.
    export SFO_STRATEGY_FAST_PUBLICATION=1
    # Build in an isolated directory without the operational lock, then take the
    # lock only for atomic artifact promotion and the manifest rebuild.
    # This keeps the five-minute publisher from queueing behind research work.
    strategy_stage_dir="$(mktemp -d "${TMPDIR:-/tmp}/weatheredge-strategy.XXXXXX")"
    strategy_stage_output="$strategy_stage_dir/strategy_research.json"
    strategy_stage_private="$strategy_stage_dir/strategy_research_evidence.private.json"
    strategy_private_output="$(dirname "$STRATEGY_OUTPUT_PATH")/strategy_research_evidence.private.json"
    strategy_promote_tmp="${STRATEGY_OUTPUT_PATH}.promote.$$"
    strategy_private_promote_tmp="${strategy_private_output}.promote.$$"
    trap 'rm -rf "$strategy_stage_dir"; rm -f "$strategy_promote_tmp" "$strategy_private_promote_tmp"' EXIT

    SFO_STRATEGY_BUILD_STAGING=1 \
      SFO_STRATEGY_RESEARCH_PATH="$strategy_stage_output" \
      /bin/bash "$BUILDER"

    mkdir -p "$(dirname "$ARTIFACT_LOCK")" "$(dirname "$STRATEGY_OUTPUT_PATH")"
    exec 7>"$ARTIFACT_LOCK"
    if ! flock -w "$LOCK_WAIT_SECONDS" 7; then
      echo "timed out waiting to promote Strategy Lab artifact: $ARTIFACT_LOCK" >&2
      exit 1
    fi

    cp -- "$strategy_stage_output" "$strategy_promote_tmp"
    mv -f -- "$strategy_promote_tmp" "$STRATEGY_OUTPUT_PATH"
    if [[ -f "$strategy_stage_private" ]]; then
      cp -- "$strategy_stage_private" "$strategy_private_promote_tmp"
      mv -f -- "$strategy_private_promote_tmp" "$strategy_private_output"
    fi
    "$PYTHON_BIN" -m sfo_kalshi_quant.publication build \
      --artifact-root "$FORECASTER_DIR" \
      --output "$MANIFEST_OUTPUT_PATH" >/dev/null
    flock -u 7
    exec 7>&-
    echo "strategy artifact built; publication deferred to the operational cycle"
    exit 0
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

# Hand the build lock to the publisher. It releases the descriptor before the
# Pages delivery gate, then reacquires the same lock to validate and copy a
# coherent snapshot after the prior branch head is public.
export SFO_ARTIFACT_LOCK_HELD=1
export SFO_ARTIFACT_LOCK_FD=7
/bin/bash "$BUILDER"
/bin/bash "$SCRIPT_DIR/publish_forecaster_pages.sh"
