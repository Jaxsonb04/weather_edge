#!/usr/bin/env bash
# Forecast freshness watchdog (AWS box).
#
# Checks both forecast-state freshness and the public-artifact snapshot. A fresh
# weather.db cannot hide a frozen signal/cities artifact, and rebuilding only the
# manifest cannot reset the operational clocks recorded inside it.
#
# Wire a real push alert by setting SFO_FRESHNESS_ALERT_URL in /etc/weatheredge.env
# to a plain-text HTTP endpoint, such as an ntfy.sh topic.
set -uo pipefail

FORECASTER_DIR="${SFO_FORECASTER_ROOT:-/opt/weatheredge/forecaster}"
TRADING_DIR="${SFO_TRADING_ROOT:-/opt/weatheredge/trading}"
PYTHON_BIN="${SFO_TRADING_PYTHON:-$TRADING_DIR/.venv/bin/python}"
DB="${SFO_FORECAST_DB:-$FORECASTER_DIR/weather.db}"
MAX_AGE_HOURS="${SFO_FORECAST_MAX_AGE_HOURS:-6}"
MARKER="${SFO_FORECAST_STALE_MARKER:-$FORECASTER_DIR/STALE_FORECAST}"
ALERT_URL="${SFO_FRESHNESS_ALERT_URL:-}"
MANIFEST="${SFO_PUBLICATION_MANIFEST_PATH:-$FORECASTER_DIR/publication_manifest.json}"
OPERATIONAL_MAX_MINUTES="${SFO_PUBLICATION_MAX_OPERATIONAL_AGE_MINUTES:-10}"
STRATEGY_MAX_MINUTES="${SFO_PUBLICATION_MAX_STRATEGY_AGE_MINUTES:-20}"
PUBLIC_MANIFEST_URL="${SFO_PUBLICATION_MANIFEST_URL:-${SFO_PUBLIC_MANIFEST_URL:-}}"
PUBLISH_PAGES="${SFO_PUBLISH_PAGES:-0}"

now=$(date +%s)
failures=()

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
if ! local_manifest_result="$(
  cd "$TRADING_DIR" 2>/dev/null \
    && "$PYTHON_BIN" "${manifest_args[@]}" 2>&1
)"; then
  failures+=("local publication manifest invalid: $local_manifest_result")
fi

public_manifest_result=""
if [[ -n "$PUBLIC_MANIFEST_URL" ]]; then
  public_tmp=$(mktemp "${TMPDIR:-/tmp}/weatheredge-public-manifest.XXXXXX")
  if ! curl -fsS -m 15 "$PUBLIC_MANIFEST_URL" >"$public_tmp"; then
    failures+=("public publication manifest unavailable: $PUBLIC_MANIFEST_URL")
  elif ! public_manifest_result="$(
    cd "$TRADING_DIR" 2>/dev/null \
      && "$PYTHON_BIN" -m sfo_kalshi_quant.publication validate-metadata \
        --manifest "$public_tmp" \
        --require-strategy \
        --max-operational-age-minutes "$OPERATIONAL_MAX_MINUTES" \
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
  if [[ -n "$ALERT_URL" ]]; then
    if curl -fsS -m 15 -X POST -H "Content-Type: text/plain" \
        --data "WeatherEdge ALERT: $msg" "$ALERT_URL" >/dev/null 2>&1; then
      echo "alert posted to SFO_FRESHNESS_ALERT_URL"
    else
      echo "alert POST failed (check SFO_FRESHNESS_ALERT_URL)" >&2
    fi
  fi
  exit 1
fi

rm -f "$MARKER" 2>/dev/null || true
echo "OK: forecast DB fresh (${age_h}h old, threshold ${MAX_AGE_HOURS}h)"
echo "OK: publication manifest valid (operational <=${OPERATIONAL_MAX_MINUTES}m, strategy <=${STRATEGY_MAX_MINUTES}m)"
if [[ -n "$PUBLIC_MANIFEST_URL" ]]; then
  echo "OK: public publication manifest valid ($PUBLIC_MANIFEST_URL)"
fi
exit 0
