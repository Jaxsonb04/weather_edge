#!/usr/bin/env bash
set -euo pipefail

# Skip this tick if a previous scan is still running. The 5-minute timer can fire
# before a slow scan (portfolio-scan across both profiles)
# finishes; WAL keeps the DB consistent but does not stop two scans doing
# duplicate logical work or both placing paper entries. flock is a no-op where
# unavailable (local macOS dev).
TRADING_DIR="${SFO_TRADING_ROOT:-/opt/weatheredge/trading}"
BASE_DIR="${SFO_BASE_DIR:-${BASE_DIR:-$(dirname "$TRADING_DIR")}}"
SCAN_LOCK="${SFO_PAPER_SCAN_LOCK:-$BASE_DIR/.locks/paper-scan.lock}"
if command -v flock >/dev/null 2>&1; then
  mkdir -p "$(dirname "$SCAN_LOCK")"
  exec 9>"$SCAN_LOCK"
  if ! flock -n 9; then
    echo "previous paper scan still running; skipping this tick"
    exit 0
  fi
fi

FORECASTER_DIR="${SFO_FORECASTER_ROOT:-/opt/weatheredge/forecaster}"
PYTHON_BIN="${SFO_TRADING_PYTHON:-$TRADING_DIR/.venv/bin/python}"
DB_PATH="${SFO_KALSHI_DB:-$TRADING_DIR/data/paper_trading.db}"
BANKROLL="${PAPER_BANKROLL:-1000}"
PROFILES_CSV="${PAPER_RISK_PROFILES:-${PAPER_RISK_PROFILE:-live}}"
CALIBRATION_SOURCE="${SFO_TRADING_SIGNAL_CALIBRATION_SOURCE:-lstm}"
PAPER_ENTRY_MODE="${PAPER_ENTRY_MODE:-market}"
TARGET_DATE="${SFO_PAPER_SCAN_TARGET_DATE:-rolling}"
SIDE="${SFO_PAPER_SCAN_SIDE:-both}"
PORTFOLIO_MAX_ARB_SPEND="${SFO_PORTFOLIO_MAX_ARB_SPEND:-12}"
PORTFOLIO_MIN_PROFIT="${SFO_PORTFOLIO_MIN_PROFIT:-0.01}"
PAPER_PLACE_LIVE="${PAPER_PLACE_LIVE:-0}"
PAPER_PLACE_RESEARCH_TARGET="${PAPER_PLACE_RESEARCH_TARGET:-0}"
PAPER_PLACE_RESEARCH_MOTION="${PAPER_PLACE_RESEARCH_MOTION:-0}"

truthy() {
  value="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$value" in
    1 | true | yes | y | on) return 0 ;;
    *) return 1 ;;
  esac
}

if [[ "$PYTHON_BIN" != */* ]]; then
  if ! PYTHON_BIN="$(command -v "$PYTHON_BIN")"; then
    echo "missing trading Python runtime: $SFO_TRADING_PYTHON" >&2
    exit 1
  fi
elif [[ ! -x "$PYTHON_BIN" ]]; then
  echo "missing trading Python runtime: $PYTHON_BIN" >&2
  exit 1
fi

cd "$TRADING_DIR"

IFS=',' read -r -a profiles <<< "$PROFILES_CSV"
profile_index=0
for raw_profile in "${profiles[@]}"; do
  profile="${raw_profile//[[:space:]]/}"
  if [[ -z "$profile" ]]; then
    continue
  fi

  skip_context=0
  if (( profile_index > 0 )); then
    skip_context=1
  fi

  args=(
    --no-color
    --forecaster-root "$FORECASTER_DIR"
    --db-path "$DB_PATH"
    --bankroll "$BANKROLL"
    --risk-profile "$profile"
    portfolio-scan
    --target-date "$TARGET_DATE"
    --side "$SIDE"
    --calibration-source "$CALIBRATION_SOURCE"
    --paper-entry-mode "$PAPER_ENTRY_MODE"
    --max-arb-spend "$PORTFOLIO_MAX_ARB_SPEND"
    --min-profit "$PORTFOLIO_MIN_PROFIT"
  )
  case "$profile" in
    live)
      if truthy "$PAPER_PLACE_LIVE"; then
        args+=(--place-paper)
      else
        echo "live allocator shadow mode: recording decisions without paper placement"
      fi
      ;;
    research)
      if truthy "$PAPER_PLACE_RESEARCH_TARGET"; then
        args+=(--place-research-target)
      fi
      if truthy "$PAPER_PLACE_RESEARCH_MOTION"; then
        args+=(--place-research-motion)
      fi
      if ! truthy "$PAPER_PLACE_RESEARCH_TARGET" && ! truthy "$PAPER_PLACE_RESEARCH_MOTION"; then
        echo "research allocators shadow mode: recording decisions without paper placement"
      fi
      ;;
    *)
      echo "unknown profile has no placement control and remains shadow-only: $profile" >&2
      ;;
  esac
  # Forecast/probability/market context is identical across profiles in one
  # scan; only the first profile's first command records it.
  if (( skip_context > 0 )); then
    args+=(--skip-context-snapshots)
  fi
  profile_index=$((profile_index + 1))

  echo "running portfolio paper scan profile=$profile db=$DB_PATH"
  "$PYTHON_BIN" -m sfo_kalshi_quant.cli "${args[@]}"
done
