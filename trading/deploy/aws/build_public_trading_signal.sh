#!/usr/bin/env bash
set -euo pipefail

FORECASTER_DIR="${SFO_FORECASTER_ROOT:-/opt/weatheredge/forecaster}"
TRADING_DIR="${SFO_TRADING_ROOT:-/opt/weatheredge/trading}"
PYTHON_BIN="${SFO_TRADING_PYTHON:-$TRADING_DIR/.venv/bin/python}"
DB_PATH="${SFO_KALSHI_DB:-$TRADING_DIR/data/paper_trading.db}"
OUTPUT_PATH="${SFO_TRADING_SIGNAL_PATH:-$FORECASTER_DIR/trading_signal.json}"
RESEARCH_OUTPUT_PATH="${SFO_STRATEGY_RESEARCH_PATH:-$FORECASTER_DIR/strategy_research.json}"
TARGET_DATE="${SFO_TRADING_SIGNAL_TARGET_DATE:-both}"
SIDE="${SFO_TRADING_SIGNAL_SIDE:-both}"
ENSEMBLE_TIMEOUT="${SFO_TRADING_SIGNAL_ENSEMBLE_TIMEOUT:-12}"
CALIBRATION_SOURCE="${SFO_TRADING_SIGNAL_CALIBRATION_SOURCE:-lstm}"
CALIBRATION_MIN_TRAIN="${SFO_STRATEGY_RESEARCH_CALIBRATION_MIN_TRAIN:-180}"

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

mkdir -p "$(dirname "$OUTPUT_PATH")"
mkdir -p "$(dirname "$RESEARCH_OUTPUT_PATH")"

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

CITIES_OUTPUT_PATH="${SFO_CITIES_DATA_PATH:-$FORECASTER_DIR/cities_data.json}"

cd "$TRADING_DIR"
"$PYTHON_BIN" -m sfo_kalshi_quant.cli "${args[@]}" >/dev/null
"$PYTHON_BIN" -m sfo_kalshi_quant.cities_report \
  --forecaster-root "$FORECASTER_DIR" \
  --db-path "$DB_PATH" \
  --output "$CITIES_OUTPUT_PATH" >/dev/null
"$PYTHON_BIN" -m sfo_kalshi_quant.cli \
  --no-color \
  --forecaster-root "$FORECASTER_DIR" \
  --db-path "$DB_PATH" \
  strategy-research \
  --calibration-min-train "$CALIBRATION_MIN_TRAIN" \
  --output "$RESEARCH_OUTPUT_PATH" >/dev/null
