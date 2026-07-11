#!/usr/bin/env bash
set -euo pipefail

TIMERS=(
  sfo-forecaster-refresh.timer
  sfo-operational-publish.timer
  sfo-strategy-lab-refresh.timer
  sfo-dataset-backfill.timer
  sfo-kalshi-paper-scan.timer
  sfo-kalshi-paper-monitor.timer
  sfo-kalshi-paper-settle.timer
  sfo-kalshi-paper-prune.timer
  sfo-forecast-freshness.timer
)

if [[ -n "${SYSTEMCTL_BIN:-}" ]]; then
  SYSTEMCTL=("$SYSTEMCTL_BIN")
else
  SYSTEMCTL=(sudo systemctl)
fi

for timer in "${TIMERS[@]}"; do
  unit_files=""
  if unit_files="$("${SYSTEMCTL[@]}" list-unit-files "$timer" --no-legend)"; then
    :
  else
    status=$?
    echo "failed to inspect systemd timer: $timer" >&2
    exit "$status"
  fi
  if grep -q "^$timer" <<<"$unit_files"; then
    "${SYSTEMCTL[@]}" stop "$timer"
    "${SYSTEMCTL[@]}" disable "$timer"
  fi
done

echo "existing WeatherEdge timers stopped and disabled"
