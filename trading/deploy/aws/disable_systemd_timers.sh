#!/usr/bin/env bash
set -euo pipefail

UNIT_PAIRS=(
  "sfo-forecaster-refresh.timer sfo-forecaster-refresh.service"
  "sfo-operational-publish.timer sfo-operational-publish.service"
  "sfo-strategy-lab-refresh.timer sfo-strategy-lab-refresh.service"
  "sfo-dataset-backfill.timer sfo-dataset-backfill.service"
  "sfo-kalshi-paper-scan.timer sfo-kalshi-paper-scan.service"
  "sfo-kalshi-paper-monitor.timer sfo-kalshi-paper-monitor.service"
  "sfo-kalshi-paper-settle.timer sfo-kalshi-paper-settle.service"
  "sfo-kalshi-paper-prune.timer sfo-kalshi-paper-prune.service"
  "sfo-forecast-freshness.timer sfo-forecast-freshness.service"
)

if [[ -n "${SYSTEMCTL_BIN:-}" ]]; then
  SYSTEMCTL=("$SYSTEMCTL_BIN")
else
  SYSTEMCTL=(sudo systemctl)
fi

inspect_unit() {
  local unit="$1"
  local load_state=""
  local status=0
  if load_state="$("${SYSTEMCTL[@]}" show --property=LoadState --value "$unit")"; then
    :
  else
    status=$?
    echo "failed to inspect systemd unit: $unit" >&2
    exit "$status"
  fi
  [[ -n "$load_state" && "$load_state" != "not-found" ]]
}

for pair in "${UNIT_PAIRS[@]}"; do
  read -r timer service <<<"$pair"
  if inspect_unit "$timer"; then
    "${SYSTEMCTL[@]}" stop "$timer"
    "${SYSTEMCTL[@]}" disable "$timer"
  fi
  if inspect_unit "$service"; then
    "${SYSTEMCTL[@]}" stop "$service"
    service_state=""
    service_status=0
    service_state="$("${SYSTEMCTL[@]}" is-active "$service")" || service_status=$?
    if [[ ( "$service_state" != "inactive" && "$service_state" != "failed" ) || "$service_status" -ne 3 ]]; then
      echo "systemd service remains active or unverifiable: $service ($service_state)" >&2
      exit 1
    fi
  fi
done

echo "existing WeatherEdge timers disabled and paired services inactive"
