#!/usr/bin/env bash
set -euo pipefail

FORECASTER_DIR="${SFO_FORECASTER_ROOT:-/opt/weatheredge/forecaster}"
TRADING_DIR="${SFO_TRADING_ROOT:-/opt/weatheredge/trading}"
PYTHON_BIN="${SFO_TRADING_PYTHON:-$TRADING_DIR/.venv/bin/python}"
DB_PATH="${SFO_KALSHI_DB:-$TRADING_DIR/data/paper_trading.db}"
RESEARCH_OUTPUT_PATH="${SFO_STRATEGY_RESEARCH_PATH:-$FORECASTER_DIR/strategy_research.json}"
CALIBRATION_MIN_TRAIN="${SFO_STRATEGY_RESEARCH_CALIBRATION_MIN_TRAIN:-180}"
ARTIFACT_LOCK="${SFO_ARTIFACT_GENERATION_LOCK:-/opt/weatheredge/.locks/artifact-generation.lock}"
LOCK_WAIT_SECONDS="${SFO_ARTIFACT_LOCK_WAIT_SECONDS:-900}"

if [[ "$PYTHON_BIN" != */* ]]; then
  if ! PYTHON_BIN="$(command -v "$PYTHON_BIN")"; then
    echo "missing trading Python runtime: $SFO_TRADING_PYTHON" >&2
    exit 1
  fi
elif [[ ! -x "$PYTHON_BIN" ]]; then
  echo "missing trading Python runtime: $PYTHON_BIN" >&2
  exit 1
fi

if [[ ! -d "$FORECASTER_DIR" ]]; then
  echo "missing forecaster directory: $FORECASTER_DIR" >&2
  exit 1
fi

if [[ "${SFO_ARTIFACT_LOCK_HELD:-0}" != "1" ]]; then
  mkdir -p "$(dirname "$ARTIFACT_LOCK")"
  exec 9>"$ARTIFACT_LOCK"
  if ! flock -w "$LOCK_WAIT_SECONDS" 9; then
    echo "timed out waiting for artifact generation lock: $ARTIFACT_LOCK" >&2
    exit 1
  fi
  export SFO_ARTIFACT_LOCK_HELD=1
fi

mkdir -p "$(dirname "$RESEARCH_OUTPUT_PATH")"

cd "$TRADING_DIR"
"$PYTHON_BIN" -m sfo_kalshi_quant.cli \
  --no-color \
  --forecaster-root "$FORECASTER_DIR" \
  --db-path "$DB_PATH" \
  strategy-research \
  --calibration-min-train "$CALIBRATION_MIN_TRAIN" \
  --output "$RESEARCH_OUTPUT_PATH" >/dev/null
