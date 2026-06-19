#!/usr/bin/env bash
set -euo pipefail

# Skip this tick if a previous scan is still running. The 5-minute timer can fire
# before a slow scan (arbitrage + tail-basket + analyze across both profiles)
# finishes; WAL keeps the DB consistent but does not stop two scans doing
# duplicate logical work or both placing paper entries. flock is a no-op where
# unavailable (local macOS dev).
SCAN_LOCK="${SFO_PAPER_SCAN_LOCK:-${TMPDIR:-/tmp}/sfo-paper-scan.lock}"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$SCAN_LOCK"
  if ! flock -n 9; then
    echo "previous paper scan still running; skipping this tick"
    exit 0
  fi
fi

TRADING_DIR="${SFO_TRADING_ROOT:-/opt/weatheredge/trading}"
FORECASTER_DIR="${SFO_FORECASTER_ROOT:-/opt/weatheredge/forecaster}"
PYTHON_BIN="${SFO_TRADING_PYTHON:-$TRADING_DIR/.venv/bin/python}"
DB_PATH="${SFO_KALSHI_DB:-$TRADING_DIR/data/paper_trading.db}"
BANKROLL="${PAPER_BANKROLL:-1000}"
PROFILES_CSV="${PAPER_RISK_PROFILES:-${PAPER_RISK_PROFILE:-live}}"
CALIBRATION_SOURCE="${SFO_TRADING_SIGNAL_CALIBRATION_SOURCE:-lstm}"
PAPER_ENTRY_MODE="${PAPER_ENTRY_MODE:-market}"
TARGET_DATE="${SFO_PAPER_SCAN_TARGET_DATE:-rolling}"
SIDE="${SFO_PAPER_SCAN_SIDE:-both}"
ARBITRAGE_ENABLED="${SFO_PAPER_SCAN_ARBITRAGE_ENABLED:-1}"
ARBITRAGE_MAX_SPEND="${SFO_ARBITRAGE_MAX_SPEND:-12}"
ARBITRAGE_MIN_PROFIT="${SFO_ARBITRAGE_MIN_PROFIT:-0.01}"
TAIL_BASKET_ENABLED="${SFO_PAPER_SCAN_TAIL_BASKET_ENABLED:-1}"
TAIL_BASKET_DISTANCE="${SFO_TAIL_BASKET_DISTANCE:-3}"
# Volatility retune (2026-06-18): size the far-tail NO legs off the evaluator's
# risk budget (quarter-Kelly + comfort boost) instead of a hardcoded $5/leg --
# the fixed $5/$1 stakes were the dominant source of the inert "$1-2" P&L. The
# basket is still bounded by MAX_SPEND / MAX_WORST_LOSS (raised below) and still
# held to a non-negative lower-bound edge per leg. Set SFO_TAIL_BASKET_SIZING=fixed
# to revert to the old bounded-guardrail behavior.
TAIL_BASKET_SIZING="${SFO_TAIL_BASKET_SIZING:-kelly}"
TAIL_BASKET_TAIL_STAKE="${SFO_TAIL_BASKET_TAIL_STAKE:-5}"
TAIL_BASKET_CENTER_STAKE="${SFO_TAIL_BASKET_CENTER_STAKE:-1}"
TAIL_BASKET_MAX_TAIL_PROBABILITY="${SFO_TAIL_BASKET_MAX_TAIL_PROBABILITY:-0.20}"
TAIL_BASKET_MAX_SPEND="${SFO_TAIL_BASKET_MAX_SPEND:-60}"
TAIL_BASKET_MAX_WORST_LOSS="${SFO_TAIL_BASKET_MAX_WORST_LOSS:-50}"

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

  if [[ "$ARBITRAGE_ENABLED" != "0" ]]; then
    arbitrage_args=(
      --no-color
      --db-path "$DB_PATH"
      --bankroll "$BANKROLL"
      --risk-profile "$profile"
      arbitrage
      --target-date "$TARGET_DATE"
      --max-arb-spend "$ARBITRAGE_MAX_SPEND"
      --min-profit "$ARBITRAGE_MIN_PROFIT"
      --place-paper
    )
    # Arbitrage records ONLY market_snapshots -- never the forecast/probability
    # ladder (it works off raw Kalshi YES+NO box prices, with no forecast). So it
    # must NEVER be the per-tick context writer: letting a successful arbitrage run
    # flip skip_context=1 made the full-context commands below skip, which silently
    # starved forecast_snapshots + probability_snapshots from 2026-06-16 (blinding
    # the dashboard calibration and the legacy stop-loss model-veto that read them).
    # Always skip context here; ownership stays with the full-context commands
    # (tail-basket, else the always-run analyzer).
    arbitrage_args+=(--skip-context-snapshots)

    echo "running arbitrage scan profile=$profile db=$DB_PATH"
    if ! "$PYTHON_BIN" -m sfo_kalshi_quant.cli "${arbitrage_args[@]}"; then
      echo "warning: arbitrage scan failed for profile=$profile; continuing with broad scan" >&2
    fi
  fi

  if [[ "$TAIL_BASKET_ENABLED" != "0" ]]; then
    basket_args=(
      --no-color
      --forecaster-root "$FORECASTER_DIR"
      --db-path "$DB_PATH"
      --bankroll "$BANKROLL"
      --risk-profile "$profile"
      tail-basket
      --target-date "$TARGET_DATE"
      --calibration-source "$CALIBRATION_SOURCE"
      --tail-distance "$TAIL_BASKET_DISTANCE"
      --basket-sizing "$TAIL_BASKET_SIZING"
      --tail-stake "$TAIL_BASKET_TAIL_STAKE"
      --center-stake "$TAIL_BASKET_CENTER_STAKE"
      --max-tail-probability "$TAIL_BASKET_MAX_TAIL_PROBABILITY"
      --max-basket-spend "$TAIL_BASKET_MAX_SPEND"
      --max-worst-case-loss "$TAIL_BASKET_MAX_WORST_LOSS"
      --place-paper
    )
    if (( skip_context > 0 )); then
      basket_args+=(--skip-context-snapshots)
    fi

    echo "running tail basket profile=$profile db=$DB_PATH"
    # The experimental basket must never block the broad analyzer: a missing
    # forecast or transient Kalshi error exits non-zero and set -e would
    # otherwise abort the remaining profiles.
    if ! "$PYTHON_BIN" -m sfo_kalshi_quant.cli "${basket_args[@]}"; then
      echo "warning: tail basket failed for profile=$profile; continuing with broad scan" >&2
    else
      # Only suppress the analyzer's context snapshots when the basket
      # actually recorded them.
      skip_context=1
    fi
  fi

  args=(
    --no-color
    --forecaster-root "$FORECASTER_DIR"
    --db-path "$DB_PATH"
    --bankroll "$BANKROLL"
    --risk-profile "$profile"
    analyze
    --target-date "$TARGET_DATE"
    --side "$SIDE"
    --calibration-source "$CALIBRATION_SOURCE"
    --paper-entry-mode "$PAPER_ENTRY_MODE"
    --place-paper
  )
  # Forecast/probability/market context is identical across profiles in one
  # scan; only the first profile's first command records it.
  if (( skip_context > 0 )); then
    args+=(--skip-context-snapshots)
  fi
  profile_index=$((profile_index + 1))
  if [[ -n "${PAPER_SCAN_STAKE:-}" ]]; then
    args+=(--paper-stake "$PAPER_SCAN_STAKE")
  fi
  if [[ -n "${PAPER_SCAN_DAILY_BUDGET:-}" ]]; then
    args+=(--daily-budget "$PAPER_SCAN_DAILY_BUDGET")
  fi

  echo "running paper scan profile=$profile db=$DB_PATH"
  "$PYTHON_BIN" -m sfo_kalshi_quant.cli "${args[@]}"
done
