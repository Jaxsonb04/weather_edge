#!/usr/bin/env bash
# Forecast freshness watchdog (AWS box).
#
# Checks both forecast-state freshness and the public-artifact snapshot. A fresh
# weather.db cannot hide a frozen signal/cities artifact, and rebuilding only the
# manifest cannot reset the operational clocks recorded inside it.
#
# Under systemd, a nonzero exit triggers the shared sfo-alert@.service JSON
# webhook. Manual runs only report failures locally and never post a duplicate.
set -uo pipefail

BASE_DIR="${SFO_BASE_DIR:-${BASE_DIR:-/opt/weatheredge}}"
FORECASTER_DIR="${SFO_FORECASTER_ROOT:-$BASE_DIR/forecaster}"
TRADING_DIR="${SFO_TRADING_ROOT:-$BASE_DIR/trading}"
PYTHON_BIN="${SFO_TRADING_PYTHON:-$TRADING_DIR/.venv/bin/python}"
DB="${SFO_FORECAST_DB:-$FORECASTER_DIR/weather.db}"
MAX_AGE_HOURS="${SFO_FORECAST_MAX_AGE_HOURS:-6}"
MARKER="${SFO_FORECAST_STALE_MARKER:-$FORECASTER_DIR/STALE_FORECAST}"
MANIFEST="${SFO_PUBLICATION_MANIFEST_PATH:-$FORECASTER_DIR/publication_manifest.json}"
OPERATIONAL_MAX_MINUTES="${SFO_PUBLICATION_MAX_OPERATIONAL_AGE_MINUTES:-10}"
# Audit F-01: this watchdog measures the same public artifact age as
# check_scheduler_health.sh and must use the same ceiling. Public age is
# measured from the artifact's own generated_at, so with a five-minute
# publish cadence and a ~five-minute Pages deployment the artifact visitors
# receive is ~5-11 minutes old for the whole interval between landings. Left
# at 10 this fires the shared alert webhook continuously by construction.
PUBLIC_OPERATIONAL_MAX_MINUTES="${SFO_PUBLICATION_MAX_PUBLIC_OPERATIONAL_AGE_MINUTES:-20}"
STRATEGY_MAX_MINUTES="${SFO_PUBLICATION_MAX_STRATEGY_AGE_MINUTES:-20}"
# Historical-analysis age is deliberately not an operational watchdog input.
# Strategy Lab itself fails stale readiness evidence closed; this fast service
# cannot regenerate the immutable-snapshot analysis and must not claim it can.
PUBLIC_MANIFEST_URL="${SFO_PUBLICATION_MANIFEST_URL:-${SFO_PUBLIC_MANIFEST_URL:-}}"
PUBLISH_PAGES="${SFO_PUBLISH_PAGES:-0}"
DISK_MAX_PERCENT="${SFO_DISK_USAGE_MAX_PERCENT:-85}"
ARTIFACT_LOCK="${SFO_ARTIFACT_GENERATION_LOCK:-$BASE_DIR/.locks/artifact-generation.lock}"
ARTIFACT_LOCK_WAIT_SECONDS="${SFO_FRESHNESS_ARTIFACT_LOCK_WAIT_SECONDS:-120}"

now=$(date +%s)
failures=()
disk_percent="n/a"

if [[ ! "$DISK_MAX_PERCENT" =~ ^[0-9]+$ ]] || (( DISK_MAX_PERCENT < 1 || DISK_MAX_PERCENT > 100 )); then
  failures+=("disk usage threshold malformed: $DISK_MAX_PERCENT")
elif ! disk_output="$(df -P "$BASE_DIR" 2>&1)"; then
  failures+=("disk usage check failed for $BASE_DIR: $disk_output")
else
  disk_field="$(printf '%s\n' "$disk_output" | awk 'NR == 2 { print $5 }')"
  if [[ ! "$disk_field" =~ ^[0-9]+%$ ]]; then
    failures+=("disk usage output malformed for $BASE_DIR: ${disk_field:-missing percentage}")
  else
    disk_percent="${disk_field%%%}"
    if (( disk_percent >= DISK_MAX_PERCENT )); then
      failures+=("disk usage ${disk_percent}% at $BASE_DIR reached threshold ${DISK_MAX_PERCENT}%")
    fi
  fi
fi

if [[ "$PUBLISH_PAGES" == "1" && -z "$PUBLIC_MANIFEST_URL" ]]; then
  failures+=("SFO_PUBLICATION_MANIFEST_URL is required when SFO_PUBLISH_PAGES=1")
fi

if [[ ! -f "$DB" ]]; then
  age_h="n/a"
  failures+=("forecast DB missing: $DB")
else
  mtime=$(stat -c %Y "$DB" 2>/dev/null || stat -f %m "$DB")
  age_s=$(( now - mtime ))
  age_h=$(awk "BEGIN{printf \"%.1f\", $age_s/3600}")
  max_s=$(awk "BEGIN{printf \"%d\", $MAX_AGE_HOURS*3600}")
  if (( age_s > max_s )); then
    failures+=("forecast DB stale: $DB is ${age_h}h old (threshold ${MAX_AGE_HOURS}h)")
  fi
fi

manifest_args=(
  -m sfo_kalshi_quant.publication validate
  --artifact-root "$FORECASTER_DIR"
  --manifest "$MANIFEST"
  --require-strategy
  --max-operational-age-minutes "$OPERATIONAL_MAX_MINUTES"
  --max-strategy-age-minutes "$STRATEGY_MAX_MINUTES"
)
validate_local_manifest() {
  cd "$TRADING_DIR" 2>/dev/null \
    && "$PYTHON_BIN" "${manifest_args[@]}"
}
if ! [[ "$ARTIFACT_LOCK_WAIT_SECONDS" =~ ^[0-9]+$ ]]; then
  failures+=("artifact lock wait malformed: $ARTIFACT_LOCK_WAIT_SECONDS")
elif command -v flock >/dev/null 2>&1; then
  mkdir -p "$(dirname "$ARTIFACT_LOCK")"
  exec 8>"$ARTIFACT_LOCK"
  if ! flock -w "$ARTIFACT_LOCK_WAIT_SECONDS" 8; then
    failures+=(
      "local publication manifest invalid: artifact lock remained busy after ${ARTIFACT_LOCK_WAIT_SECONDS}s"
    )
  elif ! local_manifest_result="$(validate_local_manifest 2>&1)"; then
    failures+=("local publication manifest invalid: $local_manifest_result")
  fi
elif ! local_manifest_result="$(validate_local_manifest 2>&1)"; then
  failures+=("local publication manifest invalid: $local_manifest_result")
fi

public_manifest_result=""
if [[ -n "$PUBLIC_MANIFEST_URL" ]]; then
  public_tmp=$(mktemp "${TMPDIR:-/tmp}/weatheredge-public-manifest.XXXXXX")
  cache_bust_separator="?"
  [[ "$PUBLIC_MANIFEST_URL" == *"?"* ]] && cache_bust_separator="&"
  public_manifest_poll_url="${PUBLIC_MANIFEST_URL}${cache_bust_separator}poll=$(date +%s%N)"
  if ! curl -fsS -m 15 "$public_manifest_poll_url" >"$public_tmp"; then
    failures+=("public publication manifest unavailable: $PUBLIC_MANIFEST_URL")
  elif ! public_manifest_result="$(
    cd "$TRADING_DIR" 2>/dev/null \
      && "$PYTHON_BIN" -m sfo_kalshi_quant.publication validate-metadata \
        --manifest "$public_tmp" \
        --require-strategy \
        --max-operational-age-minutes "$PUBLIC_OPERATIONAL_MAX_MINUTES" \
        --max-strategy-age-minutes "$STRATEGY_MAX_MINUTES" 2>&1
  )"; then
    failures+=("public publication manifest invalid: $public_manifest_result")
  fi
  rm -f "$public_tmp"
fi

if (( ${#failures[@]} > 0 )); then
  msg="$(IFS='; '; echo "${failures[*]}")"
  echo "STALE: $msg" >&2
  {
    date -u +%Y-%m-%dT%H:%M:%SZ
    echo "$msg"
  } > "$MARKER" 2>/dev/null || true
  exit 1
fi

rm -f "$MARKER" 2>/dev/null || true
echo "OK: forecast DB fresh (${age_h}h old, threshold ${MAX_AGE_HOURS}h)"
echo "OK: publication manifest valid (operational <=${OPERATIONAL_MAX_MINUTES}m, strategy <=${STRATEGY_MAX_MINUTES}m)"
echo "OK: disk usage ${disk_percent}% at $BASE_DIR (threshold <${DISK_MAX_PERCENT}%)"
if [[ -n "$PUBLIC_MANIFEST_URL" ]]; then
  echo "OK: public publication manifest valid ($PUBLIC_MANIFEST_URL)"
fi
exit 0
