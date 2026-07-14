#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-quiesce}"
if (( $# > 0 )); then
  shift
fi

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

known_timer() {
  local candidate="$1"
  local pair=""
  local timer=""
  local service=""
  for pair in "${UNIT_PAIRS[@]}"; do
    read -r timer service <<<"$pair"
    if [[ "$candidate" == "$timer" ]]; then
      return 0
    fi
  done
  return 1
}

case "$MODE" in
  capture)
    for pair in "${UNIT_PAIRS[@]}"; do
      read -r timer service <<<"$pair"
      if inspect_unit "$timer" && "${SYSTEMCTL[@]}" is-enabled --quiet "$timer"; then
        echo "$timer"
      fi
    done
    ;;
  quiesce)
    if (( $# > 0 )); then
      echo "quiesce mode does not accept timer arguments" >&2
      exit 2
    fi
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
    ;;
  restore)
    restored=()
    for timer in "$@"; do
      if ! known_timer "$timer"; then
        echo "refusing to restore unknown WeatherEdge timer: $timer" >&2
        exit 2
      fi
      restored+=("$timer")
    done
    if (( ${#restored[@]} > 0 )); then
      "${SYSTEMCTL[@]}" enable --now "${restored[@]}"
      for timer in "${restored[@]}"; do
        if ! "${SYSTEMCTL[@]}" is-active --quiet "$timer"; then
          echo "restored WeatherEdge timer is not active: $timer" >&2
          exit 1
        fi
      done
    fi
    echo "restored ${#restored[@]} previously enabled WeatherEdge timer(s)"
    ;;
  *)
    echo "usage: $0 [capture|quiesce|restore [timer ...]]" >&2
    exit 2
    ;;
esac
