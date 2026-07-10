#!/usr/bin/env bash
set -euo pipefail

FORECASTER_DIR="${SFO_FORECASTER_ROOT:-/opt/weatheredge/forecaster}"
TRADING_DIR="${SFO_TRADING_ROOT:-/opt/weatheredge/trading}"
PYTHON_BIN="${SFO_TRADING_PYTHON:-$TRADING_DIR/.venv/bin/python}"
DB_PATH="${SFO_KALSHI_DB:-$TRADING_DIR/data/paper_trading.db}"
OUTPUT_PATH="${SFO_TRADING_SIGNAL_PATH:-$FORECASTER_DIR/trading_signal.json}"
CITIES_OUTPUT_PATH="${SFO_CITIES_DATA_PATH:-$FORECASTER_DIR/cities_data.json}"
MANIFEST_OUTPUT_PATH="${SFO_PUBLICATION_MANIFEST_PATH:-$FORECASTER_DIR/publication_manifest.json}"
TARGET_DATE="${SFO_TRADING_SIGNAL_TARGET_DATE:-both}"
SIDE="${SFO_TRADING_SIGNAL_SIDE:-both}"
ENSEMBLE_TIMEOUT="${SFO_TRADING_SIGNAL_ENSEMBLE_TIMEOUT:-12}"
CALIBRATION_SOURCE="${SFO_TRADING_SIGNAL_CALIBRATION_SOURCE:-lstm}"
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

mkdir -p "$(dirname "$OUTPUT_PATH")"
mkdir -p "$(dirname "$CITIES_OUTPUT_PATH")"
mkdir -p "$(dirname "$MANIFEST_OUTPUT_PATH")"

args=(
  --no-color
  --forecaster-root "$FORECASTER_DIR"
  --db-path "$DB_PATH"
  daily-report
  --target-date "$TARGET_DATE"
  --side "$SIDE"
  --calibration-source "$CALIBRATION_SOURCE"
  --format json
  --ensemble-timeout "$ENSEMBLE_TIMEOUT"
  --output "$OUTPUT_PATH"
)

if [[ "${SFO_TRADING_SIGNAL_DISABLE_ENSEMBLE:-0}" == "1" ]]; then
  args+=(--no-ensemble)
fi

cd "$TRADING_DIR"
"$PYTHON_BIN" -m sfo_kalshi_quant.cli "${args[@]}" >/dev/null
"$PYTHON_BIN" -m sfo_kalshi_quant.cities_report \
  --forecaster-root "$FORECASTER_DIR" \
  --db-path "$DB_PATH" \
  --output "$CITIES_OUTPUT_PATH" >/dev/null
"$PYTHON_BIN" -m sfo_kalshi_quant.publication build \
  --artifact-root "$FORECASTER_DIR" \
  --output "$MANIFEST_OUTPUT_PATH" >/dev/null
