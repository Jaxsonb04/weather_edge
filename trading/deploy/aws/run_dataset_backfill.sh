#!/usr/bin/env bash
set -euo pipefail

TRADING_DIR="${SFO_TRADING_ROOT:-/opt/weatheredge/trading}"
PYTHON_BIN="${SFO_TRADING_PYTHON:-$TRADING_DIR/.venv/bin/python}"
DB_PATH="${SFO_DATASET_DB:-${SFO_KALSHI_DB:-$TRADING_DIR/data/paper_trading.db}}"
SFO_DATASET_SOURCES="${SFO_DATASET_SOURCES:-iem-asos,open-meteo-previous-runs,open-meteo-historical-forecast,lamp,gfs-mos,nbm,hrrr,kalshi-history}"
LOOKBACK_DAYS="${SFO_DATASET_LOOKBACK_DAYS:-10}"
KALSHI_LOOKBACK_DAYS="${SFO_DATASET_KALSHI_LOOKBACK_DAYS:-90}"
TIMEOUT="${SFO_DATASET_TIMEOUT:-30}"
OPEN_METEO_MODEL="${SFO_DATASET_OPEN_METEO_MODEL:-best_match}"
PREVIOUS_DAYS="${SFO_DATASET_PREVIOUS_DAYS:-7}"
KALSHI_CANDLE_INTERVAL="${SFO_DATASET_KALSHI_CANDLE_INTERVAL:-60}"
KALSHI_MAX_PAGES="${SFO_DATASET_KALSHI_MAX_PAGES:-20}"
KALSHI_MAX_TRADE_PAGES="${SFO_DATASET_KALSHI_MAX_TRADE_PAGES:-1}"
LOCK_RETRY_ATTEMPTS="${SFO_DATASET_LOCK_RETRY_ATTEMPTS:-3}"
LOCK_RETRY_DELAY_SECONDS="${SFO_DATASET_LOCK_RETRY_DELAY_SECONDS:-5}"
LOCK_RETRY_BUDGET_SECONDS="${SFO_DATASET_LOCK_RETRY_BUDGET_SECONDS:-600}"
MAX_LOCK_RETRY_ATTEMPTS=10
MAX_LOCK_RETRY_DELAY_SECONDS=300
# sfo-dataset-backfill.service has one 1,800-second start deadline covering this
# script plus dataset research and four sequential forecast-maintenance jobs.
# Reserve two thirds of it for normal work; lock recovery may consume at most
# the remaining third across the entire source batch.
DATASET_SERVICE_TIMEOUT_SECONDS=1800
DATASET_SERVICE_HEADROOM_SECONDS=1200
MAX_LOCK_RETRY_BUDGET_SECONDS=$((DATASET_SERVICE_TIMEOUT_SECONDS - DATASET_SERVICE_HEADROOM_SECONDS))
SQLITE_BUSY_TIMEOUT_MILLISECONDS=30000

if [[ ! "$LOCK_RETRY_ATTEMPTS" =~ ^[1-9][0-9]?$ ]] ||
  (( 10#$LOCK_RETRY_ATTEMPTS > MAX_LOCK_RETRY_ATTEMPTS )); then
  echo "SFO_DATASET_LOCK_RETRY_ATTEMPTS must be a canonical integer from 1 to $MAX_LOCK_RETRY_ATTEMPTS" >&2
  exit 2
fi
LOCK_RETRY_ATTEMPTS=$((10#$LOCK_RETRY_ATTEMPTS))

invalid_retry_delay=0
if [[ "$LOCK_RETRY_DELAY_SECONDS" =~ ^([0-9]|[1-9][0-9]{1,2})([.][0-9]{1,3})?$ ]]; then
  retry_delay_whole_seconds="${BASH_REMATCH[1]}"
  retry_delay_fraction="${BASH_REMATCH[2]:-}"
  if (( 10#$retry_delay_whole_seconds > MAX_LOCK_RETRY_DELAY_SECONDS )) ||
    { (( 10#$retry_delay_whole_seconds == MAX_LOCK_RETRY_DELAY_SECONDS )) &&
      [[ "$retry_delay_fraction" =~ [1-9] ]]; }; then
    invalid_retry_delay=1
  fi
else
  invalid_retry_delay=1
fi
if (( invalid_retry_delay )); then
  echo "SFO_DATASET_LOCK_RETRY_DELAY_SECONDS must be a finite number from 0 to $MAX_LOCK_RETRY_DELAY_SECONDS" >&2
  exit 2
fi

if [[ ! "$LOCK_RETRY_BUDGET_SECONDS" =~ ^([0-9]|[1-9][0-9]{1,2})$ ]] ||
  (( 10#$LOCK_RETRY_BUDGET_SECONDS > MAX_LOCK_RETRY_BUDGET_SECONDS )); then
  echo "SFO_DATASET_LOCK_RETRY_BUDGET_SECONDS must be a canonical integer from 0 to $MAX_LOCK_RETRY_BUDGET_SECONDS (service timeout ${DATASET_SERVICE_TIMEOUT_SECONDS}s minus ${DATASET_SERVICE_HEADROOM_SECONDS}s headroom)" >&2
  exit 2
fi
LOCK_RETRY_BUDGET_SECONDS=$((10#$LOCK_RETRY_BUDGET_SECONDS))

retry_delay_fraction_digits="${retry_delay_fraction#.}"
retry_delay_fraction_milliseconds="${retry_delay_fraction_digits}000"
retry_delay_fraction_milliseconds="${retry_delay_fraction_milliseconds:0:3}"
LOCK_RETRY_DELAY_MILLISECONDS=$((
  10#$retry_delay_whole_seconds * 1000 + 10#$retry_delay_fraction_milliseconds
))
LOCK_RETRY_BUDGET_MILLISECONDS=$((LOCK_RETRY_BUDGET_SECONDS * 1000))

if [[ "$PYTHON_BIN" != */* ]]; then
  if ! PYTHON_BIN="$(command -v "$PYTHON_BIN")"; then
    echo "missing trading Python runtime: $SFO_TRADING_PYTHON" >&2
    exit 1
  fi
elif [[ ! -x "$PYTHON_BIN" ]]; then
  echo "missing trading Python runtime: $PYTHON_BIN" >&2
  exit 1
fi

if [[ -n "${SFO_DATASET_END_DATE:-}" ]]; then
  END_DATE="$SFO_DATASET_END_DATE"
else
  END_DATE="$(
    "$PYTHON_BIN" -c 'from datetime import datetime; from zoneinfo import ZoneInfo; import os; print(datetime.now(ZoneInfo(os.getenv("SFO_DATASET_TZ", "America/Los_Angeles"))).date().isoformat())'
  )"
fi

if [[ -n "${SFO_DATASET_START_DATE:-}" ]]; then
  START_DATE="$SFO_DATASET_START_DATE"
else
  START_DATE="$(
    SFO_DATASET_END_DATE="$END_DATE" "$PYTHON_BIN" -c 'from datetime import date, timedelta; import os; end = date.fromisoformat(os.environ["SFO_DATASET_END_DATE"]); print((end - timedelta(days=int(os.getenv("SFO_DATASET_LOOKBACK_DAYS", "10")))).isoformat())'
  )"
fi

truthy() {
  value="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$value" in
    1 | true | yes | y | on) return 0 ;;
    *) return 1 ;;
  esac
}

monotonic_milliseconds() {
  "$PYTHON_BIN" -c 'import time; print(time.monotonic_ns() // 1_000_000)'
}

LOCK_RETRY_STARTED_MILLISECONDS="$(monotonic_milliseconds)"
LOCK_RETRY_DEADLINE_MILLISECONDS=$((
  LOCK_RETRY_STARTED_MILLISECONDS + LOCK_RETRY_BUDGET_MILLISECONDS
))

run_dataset_cli_attempt() {
  local attempt_number="$1"
  shift
  if (( attempt_number > 1 )); then
    SFO_DATASET_LOCK_RETRY_DEADLINE_MILLISECONDS="$LOCK_RETRY_DEADLINE_MILLISECONDS" \
      "$PYTHON_BIN" -m sfo_kalshi_quant.cli "$@"
  else
    "$PYTHON_BIN" -m sfo_kalshi_quant.cli "$@"
  fi
}

run_dataset_source() {
  local attempt=1
  local status=0
  local now_milliseconds=0
  local elapsed_milliseconds=0
  local remaining_milliseconds=0
  local required_milliseconds=0
  while (( attempt <= LOCK_RETRY_ATTEMPTS )); do
    if run_dataset_cli_attempt "$attempt" "$@"; then
      return 0
    else
      status=$?
    fi
    if (( status != 75 || attempt >= LOCK_RETRY_ATTEMPTS )); then
      return "$status"
    fi
    now_milliseconds="$(monotonic_milliseconds)"
    elapsed_milliseconds=$((now_milliseconds - LOCK_RETRY_STARTED_MILLISECONDS))
    remaining_milliseconds=$((LOCK_RETRY_BUDGET_MILLISECONDS - elapsed_milliseconds))
    required_milliseconds=$((LOCK_RETRY_DELAY_MILLISECONDS + SQLITE_BUSY_TIMEOUT_MILLISECONDS))
    if (( remaining_milliseconds <= required_milliseconds )); then
      echo "warning: global SQLite lock retry budget exhausted; source will not be retried" >&2
      return "$status"
    fi
    echo "warning: transient SQLite lock; retrying dataset source ($attempt/$LOCK_RETRY_ATTEMPTS)" >&2
    sleep "$LOCK_RETRY_DELAY_SECONDS"
    now_milliseconds="$(monotonic_milliseconds)"
    elapsed_milliseconds=$((now_milliseconds - LOCK_RETRY_STARTED_MILLISECONDS))
    remaining_milliseconds=$((LOCK_RETRY_BUDGET_MILLISECONDS - elapsed_milliseconds))
    if (( remaining_milliseconds <= SQLITE_BUSY_TIMEOUT_MILLISECONDS )); then
      echo "warning: global SQLite lock retry budget exhausted after retry delay; source will not be retried" >&2
      return "$status"
    fi
    ((attempt += 1))
  done
  return "$status"
}

mkdir -p "$(dirname "$DB_PATH")"
cd "$TRADING_DIR"

IFS=',' read -r -a sources <<< "$SFO_DATASET_SOURCES"
failed_sources=()
for raw_source in "${sources[@]}"; do
  source="${raw_source//[[:space:]]/}"
  if [[ -z "$source" ]]; then
    continue
  fi

  source_start="$START_DATE"
  source_end="$END_DATE"
  if [[ "$source" == "kalshi-history" && -z "${SFO_DATASET_START_DATE:-}" ]]; then
    source_start="$(
      SFO_DATASET_END_DATE="$END_DATE" SFO_DATASET_KALSHI_LOOKBACK_DAYS="$KALSHI_LOOKBACK_DAYS" "$PYTHON_BIN" -c 'from datetime import date, timedelta; import os; end = date.fromisoformat(os.environ["SFO_DATASET_END_DATE"]); print((end - timedelta(days=int(os.getenv("SFO_DATASET_KALSHI_LOOKBACK_DAYS", "90")))).isoformat())'
    )"
  fi

  args=(
    --no-color
    --db-path "$DB_PATH"
    dataset-backfill
    --source "$source"
    --start-date "$source_start"
    --end-date "$source_end"
    --timeout "$TIMEOUT"
  )

  case "$source" in
    open-meteo-previous-runs)
      args+=(--open-meteo-model "$OPEN_METEO_MODEL" --previous-days "$PREVIOUS_DAYS")
      ;;
    open-meteo-historical-forecast)
      args+=(--open-meteo-model "$OPEN_METEO_MODEL")
      ;;
    kalshi-history)
      args+=(--candle-interval "$KALSHI_CANDLE_INTERVAL")
      args+=(--kalshi-max-pages "$KALSHI_MAX_PAGES")
      args+=(--kalshi-max-trade-pages "$KALSHI_MAX_TRADE_PAGES")
      if truthy "${SFO_DATASET_KALSHI_CANDLES:-0}"; then
        args+=(--kalshi-candles)
      fi
      if truthy "${SFO_DATASET_KALSHI_TRADES:-0}"; then
        args+=(--kalshi-trades)
      fi
      ;;
  esac

  echo "running dataset backfill source=$source start=$source_start end=$source_end db=$DB_PATH"
  if ! run_dataset_source "${args[@]}"; then
    failed_sources+=("$source")
    echo "warning: dataset backfill source=$source failed; continuing" >&2
  fi
done

# Evaluate the collected datasets right after every backfill so the research
# verdict (promote vs collect-only) ships with the Strategy Lab artifact
# instead of sitting unused in the DB.
FORECASTER_DIR="${SFO_FORECASTER_ROOT:-/opt/weatheredge/forecaster}"
RESEARCH_OUTPUT="${SFO_DATASET_RESEARCH_PATH:-$FORECASTER_DIR/dataset_research.json}"
echo "running dataset research output=$RESEARCH_OUTPUT"
research_failed=0
if ! "$PYTHON_BIN" -m sfo_kalshi_quant.cli \
    --no-color \
    --db-path "$DB_PATH" \
    --forecaster-root "$FORECASTER_DIR" \
    dataset-research \
    --output "$RESEARCH_OUTPUT" >/dev/null; then
  research_failed=1
  echo "ERROR: dataset research failed" >&2
fi

if [[ "${#failed_sources[@]}" -gt 0 ]]; then
  failed_list="$(IFS=,; echo "${failed_sources[*]}")"
  echo "ERROR: ${#failed_sources[@]} dataset source(s) failed: $failed_list" >&2
fi

if (( ${#failed_sources[@]} > 0 || research_failed > 0 )); then
  exit 1
fi
