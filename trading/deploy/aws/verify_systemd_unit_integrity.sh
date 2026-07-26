#!/usr/bin/env bash
# Read-only post-install gate for the canonical WeatherEdge systemd units.
# A deploy must not restart timers when runtime drop-ins, transient shadow
# units, stale fragments, or an incomplete daemon reload change their meaning.
set -euo pipefail

CANONICAL_UNIT_DIR="/etc/systemd/system"
STRATEGY_UNIT="sfo-strategy-lab-refresh.service"
STRATEGY_TIMEOUT="2min"

MANAGED_UNITS=(
  "sfo-alert@.service"
  "sfo-dataset-backfill.service"
  "sfo-forecast-freshness.service"
  "sfo-forecaster-refresh.service"
  "sfo-kalshi-paper-monitor.service"
  "sfo-kalshi-paper-prune.service"
  "sfo-kalshi-paper-scan.service"
  "sfo-kalshi-paper-settle.service"
  "sfo-operational-publish.service"
  "sfo-strategy-lab-refresh.service"
  "weatheredge-google-nonsfo-refresh.service"
  "weatheredge-google-runtime-purge.service"
  "sfo-dataset-backfill.timer"
  "sfo-forecast-freshness.timer"
  "sfo-forecaster-refresh.timer"
  "sfo-kalshi-paper-monitor.timer"
  "sfo-kalshi-paper-prune.timer"
  "sfo-kalshi-paper-scan.timer"
  "sfo-kalshi-paper-settle.timer"
  "sfo-operational-publish.timer"
  "sfo-strategy-lab-refresh.timer"
  "weatheredge-google-nonsfo-refresh.timer"
  "weatheredge-google-runtime-purge.timer"
)

if [[ -n "${SYSTEMCTL_BIN:-}" ]]; then
  SYSTEMCTL=("$SYSTEMCTL_BIN")
else
  SYSTEMCTL=(sudo systemctl)
fi

failures=0
for unit in "${MANAGED_UNITS[@]}"; do
  inspect_unit="$unit"
  if [[ "$unit" == "sfo-alert@.service" ]]; then
    # systemctl show rejects a bare template name. Inspect an unstarted
    # instance instead; systemd resolves its effective properties from the
    # canonical template while FragmentPath still names sfo-alert@.service.
    inspect_unit="sfo-alert@weatheredge-integrity.service"
  fi
  properties=""
  if properties="$(
    "${SYSTEMCTL[@]}" show \
      --property=FragmentPath \
      --property=DropInPaths \
      --property=Transient \
      --property=NeedDaemonReload \
      --property=TimeoutStartUSec \
      "$inspect_unit"
  )"; then
    :
  else
    status=$?
    echo "failed to inspect canonical systemd unit: $unit (status=$status)" >&2
    failures=$((failures + 1))
    continue
  fi

  fragment_path=""
  drop_in_paths=""
  transient=""
  need_daemon_reload=""
  timeout_start=""
  saw_fragment=0
  saw_drop_ins=0
  saw_transient=0
  saw_daemon_reload=0
  saw_timeout=0
  while IFS='=' read -r property value; do
    case "$property" in
      FragmentPath)
        fragment_path="$value"
        saw_fragment=1
        ;;
      DropInPaths)
        drop_in_paths="$value"
        saw_drop_ins=1
        ;;
      Transient)
        transient="$value"
        saw_transient=1
        ;;
      NeedDaemonReload)
        need_daemon_reload="$value"
        saw_daemon_reload=1
        ;;
      TimeoutStartUSec)
        timeout_start="$value"
        saw_timeout=1
        ;;
    esac
  done <<<"$properties"

  expected_fragment="$CANONICAL_UNIT_DIR/$unit"
  if (( saw_fragment != 1 )) || [[ "$fragment_path" != "$expected_fragment" ]]; then
    echo "$unit has unexpected FragmentPath '$fragment_path'; expected '$expected_fragment'" >&2
    failures=$((failures + 1))
  fi
  if (( saw_drop_ins != 1 )) || [[ -n "$drop_in_paths" ]]; then
    echo "$unit has unexpected DropInPaths '$drop_in_paths'; expected none" >&2
    failures=$((failures + 1))
  fi
  if (( saw_transient != 1 )) || [[ "$transient" != "no" ]]; then
    echo "$unit has unexpected Transient '$transient'; expected no" >&2
    failures=$((failures + 1))
  fi
  if (( saw_daemon_reload != 1 )) || [[ "$need_daemon_reload" != "no" ]]; then
    echo "$unit has unexpected NeedDaemonReload '$need_daemon_reload'; expected no" >&2
    failures=$((failures + 1))
  fi
  if [[ "$unit" == "$STRATEGY_UNIT" ]] \
    && { (( saw_timeout != 1 )) || [[ "$timeout_start" != "$STRATEGY_TIMEOUT" ]]; }; then
    echo "$unit has TimeoutStartUSec '$timeout_start'; expected $STRATEGY_TIMEOUT" >&2
    failures=$((failures + 1))
  fi
done

# A dummy alert instance validates the template and template-wide drop-ins,
# but systemd also permits drift scoped to one concrete alert instance.
# Reject instance fragments/drop-ins in every persistent and runtime override
# location rather than waiting for that failure path to be exercised.
if [[ -n "${SYSTEMD_UNIT_SEARCH_ROOTS:-}" ]]; then
  IFS=':' read -r -a unit_search_roots <<<"$SYSTEMD_UNIT_SEARCH_ROOTS"
else
  unit_search_roots=(
    "/etc/systemd/system"
    "/run/systemd/system"
    "/etc/systemd/system.control"
    "/run/systemd/system.control"
    "/run/systemd/transient"
  )
fi
for root in "${unit_search_roots[@]}"; do
  [[ -d "$root" ]] || continue
  shopt -s nullglob
  alert_instance_overrides=(
    "$root"/sfo-alert@?*.service
    "$root"/sfo-alert@?*.service.d
  )
  shopt -u nullglob
  for path in "${alert_instance_overrides[@]}"; do
    echo "unexpected alert-instance systemd override: $path" >&2
    failures=$((failures + 1))
  done
done

if (( failures > 0 )); then
  echo "canonical systemd unit verification failed with $failures mismatch(es)" >&2
  exit 1
fi

echo "verified ${#MANAGED_UNITS[@]} canonical systemd units with no runtime drift"
