#!/usr/bin/env bash
# Independent scheduler and publication health watchdog.
#
# This process may start only the bounded Strategy Lab refresh and operational
# publisher. It never starts paper scan/monitor/settlement services and never
# changes trading configuration. It reads the paper decision journal read-only,
# as the application user.
set -euo pipefail

INTERNAL_MODE="${1:-}"
TEST_MODE=0
if [[ "${SFO_SCHEDULER_TEST_MODE:-0}" == "1" && "$EUID" -ne 0 ]]; then
  TEST_MODE=1
fi
if (( TEST_MODE == 1 )); then
  DEPLOY_MAINTENANCE_MARKER="${SFO_DEPLOY_MAINTENANCE_MARKER:-/tmp/weatheredge-deploy-maintenance}"
  SYSTEMCTL=("${SYSTEMCTL_BIN:-systemctl}")
  INTEGRITY_HELPER="${SFO_SCHEDULER_INTEGRITY_HELPER:-/bin/true}"
  FINAL_HEALTH_CHECK="${SFO_SCHEDULER_FINAL_HEALTH_CHECK:-/bin/true}"
  PROPAGATION_WAITER="${SFO_SCHEDULER_PROPAGATION_WAITER:-/bin/true}"
  REPAIR_STATE_DIR="${SFO_SCHEDULER_REPAIR_STATE_DIR:-/tmp/weatheredge-scheduler-health}"
  # Empty skips the paper scan freshness check, like the helpers above.
  PAPER_DB="${SFO_SCHEDULER_PAPER_DB:-}"
else
  DEPLOY_MAINTENANCE_MARKER="/run/weatheredge-deploy-maintenance"
  SYSTEMCTL=(/bin/systemctl)
  INTEGRITY_HELPER="/usr/local/libexec/weatheredge/verify_systemd_unit_integrity.sh"
  REPAIR_STATE_DIR="/run/weatheredge-scheduler-health"
fi

# weatheredge-apple-refresh.timer is deliberately absent. FC-4 (2026-09-03 audit, re-verified 2026-09-06): the Apple WeatherKit refresh
# is retired. Its 4x/day paid fetch fills `AppleRuntimeCache.active_highs`,
# which has no caller anywhere outside apple_weatherkit.py; the service log says
# so itself on every run ("live trading weight remains 0"); and the 10-minute
# purge deletes the cache about an hour into each 6-hour cycle, so the data does
# not exist most of the time. It cannot become a scored EMOS member either,
# because Apple's terms do not permit retaining the archive that scoring needs.
# The unit files stay installed so re-enabling is one systemctl command.
CANONICAL_TIMERS=(
  "sfo-forecaster-refresh.timer"
  "weatheredge-google-nonsfo-refresh.timer"
  "weatheredge-apple-purge.timer"
  "weatheredge-google-runtime-purge.timer"
  "sfo-operational-publish.timer"
  "sfo-strategy-lab-refresh.timer"
  "sfo-dataset-backfill.timer"
  "sfo-kalshi-paper-scan.timer"
  "sfo-kalshi-paper-monitor.timer"
  "sfo-kalshi-paper-settle.timer"
  "sfo-kalshi-paper-prune.timer"
  "sfo-forecast-freshness.timer"
)

APP_USER="${SFO_SCHEDULER_APP_USER:-ubuntu}"
BASE_DIR="${SFO_BASE_DIR:-${BASE_DIR:-/opt/weatheredge}}"
FORECASTER_DIR="${SFO_FORECASTER_ROOT:-$BASE_DIR/forecaster}"
TRADING_DIR="${SFO_TRADING_ROOT:-$BASE_DIR/trading}"
if (( TEST_MODE == 0 )); then
  FINAL_HEALTH_CHECK="$TRADING_DIR/deploy/aws/check_forecast_db_freshness.sh"
  PROPAGATION_WAITER="$TRADING_DIR/deploy/aws/wait_for_publication_manifest.sh"
  PAPER_DB="${SFO_KALSHI_DB:-$TRADING_DIR/data/paper_trading.db}"
fi
PYTHON_BIN="${SFO_TRADING_PYTHON:-$TRADING_DIR/.venv/bin/python}"
DB="${SFO_FORECAST_DB:-$FORECASTER_DIR/weather.db}"
MANIFEST="${SFO_PUBLICATION_MANIFEST_PATH:-$FORECASTER_DIR/publication_manifest.json}"
BUILD_INFO="${SFO_BUILD_INFO_PATH:-$FORECASTER_DIR/build_info.json}"
ARTIFACT_LOCK="${SFO_ARTIFACT_GENERATION_LOCK:-$BASE_DIR/.locks/artifact-generation.lock}"
VALIDATION_LOCK_WAIT_SECONDS="${SFO_SCHEDULER_VALIDATION_LOCK_WAIT_SECONDS:-120}"
MAX_AGE_HOURS="${SFO_FORECAST_MAX_AGE_HOURS:-6}"
DISK_MAX_PERCENT="${SFO_DISK_USAGE_MAX_PERCENT:-85}"
OPERATIONAL_MAX_MINUTES="${SFO_PUBLICATION_MAX_OPERATIONAL_AGE_MINUTES:-10}"
# Public age is measured from the artifact's own generated_at. With a
# five-minute publish cadence and a ~five-minute Pages deployment, the artifact
# visitors receive is between ~5 and ~11 minutes old for the entire interval
# between landings. A 10-minute ceiling is therefore unmeetable by
# construction and produces continuous false staleness repairs.
PUBLIC_OPERATIONAL_MAX_MINUTES="${SFO_PUBLICATION_MAX_PUBLIC_OPERATIONAL_AGE_MINUTES:-20}"
STRATEGY_MAX_MINUTES="${SFO_PUBLICATION_MAX_STRATEGY_AGE_MINUTES:-20}"
# The scheduler can rebuild only the cache-backed public wrapper. Readiness
# rejects stale inner analysis itself; do not enter a repair loop for evidence
# that requires a separately bounded immutable-snapshot producer.
PUBLIC_MANIFEST_URL="${SFO_PUBLICATION_MANIFEST_URL:-${SFO_PUBLIC_MANIFEST_URL:-}}"
PUBLISH_PAGES="${SFO_PUBLISH_PAGES:-0}"
REPAIR_COOLDOWN_SECONDS="${SFO_SCHEDULER_REPAIR_COOLDOWN_SECONDS:-900}"
PROPAGATION_TIMEOUT_SECONDS="${SFO_SCHEDULER_PROPAGATION_TIMEOUT_SECONDS:-420}"
# GitHub Pages CDN propagation is variable, so a single overrun of the
# propagation deadline is a normal upstream delay rather than a scheduler
# defect. Alert only once consecutive repair cycles fail to converge; a real
# outage still surfaces one cooldown later (~15-20 minutes), while an isolated
# CDN lag resolves silently.
PROPAGATION_FAILURE_THRESHOLD="${SFO_SCHEDULER_PROPAGATION_FAILURE_THRESHOLD:-2}"
# Every paper risk profile the scan unit runs (same variables and default as
# run_paper_scan_profiles.sh) must have journaled a decision within this many
# minutes. Scans run every five minutes, so 20 tolerates two or three failed
# ticks and still fails a scan that keeps failing or hangs.
SCAN_MAX_AGE_MINUTES="${SFO_SCHEDULER_SCAN_MAX_AGE_MINUTES:-20}"
SCAN_PROFILES="${PAPER_RISK_PROFILES:-${PAPER_RISK_PROFILE:-live}}"
if (( TEST_MODE == 1 )); then
  FLOCK_BIN="${SFO_SCHEDULER_FLOCK_BIN:-flock}"
else
  FLOCK_BIN="/usr/bin/flock"
fi

run_as_app() {
  if (( EUID == 0 )) && [[ "$APP_USER" != "root" ]]; then
    /usr/sbin/runuser -u "$APP_USER" -- "$@"
  else
    "$@"
  fi
}

validate_number() {
  local name="$1"
  local value="$2"
  if [[ ! "$value" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "scheduler health configuration is invalid: $name=$value" >&2
    exit 1
  fi
}

validate_integer() {
  local name="$1"
  local value="$2"
  if [[ ! "$value" =~ ^[0-9]+$ ]]; then
    echo "scheduler health configuration is invalid: $name=$value" >&2
    exit 1
  fi
}

publication_validate() {
  (
    cd "$TRADING_DIR"
    run_as_app "$PYTHON_BIN" -m sfo_kalshi_quant.publication "$@"
  )
}

# Consecutive propagation misses are tracked as a single file inside the repair
# state directory. Absence of the file means zero, so the healthy path never has
# to create the directory or write anything.
propagation_miss_file() {
  printf '%s' "$REPAIR_STATE_DIR/propagation-miss-count"
}

read_propagation_misses() {
  local file
  file="$(propagation_miss_file)"
  local value=""
  if [[ -f "$file" ]]; then
    value="$(<"$file")"
  fi
  if [[ ! "$value" =~ ^[0-9]+$ ]]; then
    value=0
  fi
  printf '%s' "$value"
}

record_propagation_misses() {
  local value="$1"
  local file tmp
  file="$(propagation_miss_file)"
  tmp="$REPAIR_STATE_DIR/.propagation-miss-count.$$"
  printf '%s\n' "$value" >"$tmp"
  mv -f "$tmp" "$file"
}

clear_propagation_misses() {
  local file
  file="$(propagation_miss_file)"
  [[ -f "$file" ]] || return 0
  rm -f "$file"
}

validate_configuration() {
  validate_number SFO_FORECAST_MAX_AGE_HOURS "$MAX_AGE_HOURS"
  validate_number SFO_PUBLICATION_MAX_OPERATIONAL_AGE_MINUTES "$OPERATIONAL_MAX_MINUTES"
  validate_number SFO_PUBLICATION_MAX_PUBLIC_OPERATIONAL_AGE_MINUTES "$PUBLIC_OPERATIONAL_MAX_MINUTES"
  validate_number SFO_PUBLICATION_MAX_STRATEGY_AGE_MINUTES "$STRATEGY_MAX_MINUTES"
  validate_number SFO_SCHEDULER_SCAN_MAX_AGE_MINUTES "$SCAN_MAX_AGE_MINUTES"
  validate_integer SFO_DISK_USAGE_MAX_PERCENT "$DISK_MAX_PERCENT"
  validate_integer SFO_SCHEDULER_VALIDATION_LOCK_WAIT_SECONDS "$VALIDATION_LOCK_WAIT_SECONDS"
  validate_integer SFO_SCHEDULER_REPAIR_COOLDOWN_SECONDS "$REPAIR_COOLDOWN_SECONDS"
  validate_integer SFO_SCHEDULER_PROPAGATION_TIMEOUT_SECONDS "$PROPAGATION_TIMEOUT_SECONDS"
  validate_integer SFO_SCHEDULER_PROPAGATION_FAILURE_THRESHOLD "$PROPAGATION_FAILURE_THRESHOLD"
  if (( PROPAGATION_FAILURE_THRESHOLD < 1 )); then
    echo "scheduler health configuration is invalid: SFO_SCHEDULER_PROPAGATION_FAILURE_THRESHOLD=$PROPAGATION_FAILURE_THRESHOLD" >&2
    exit 1
  fi
  if (( DISK_MAX_PERCENT < 1 || DISK_MAX_PERCENT > 100 )); then
    echo "scheduler health configuration is invalid: SFO_DISK_USAGE_MAX_PERCENT=$DISK_MAX_PERCENT" >&2
    exit 1
  fi
}

validate_publication_state() (
  if [[ ! -f "$DB" ]]; then
    echo "scheduler health blocked: forecast DB missing: $DB" >&2
    exit 1
  fi
  now_epoch="$(date +%s)"
  db_mtime="$(stat -c %Y "$DB" 2>/dev/null || stat -f %m "$DB")"
  db_age_seconds=$((now_epoch - db_mtime))
  db_max_seconds="$(awk "BEGIN { printf \"%d\", $MAX_AGE_HOURS * 3600 }")"
  if (( db_age_seconds > db_max_seconds )); then
    echo "scheduler health blocked: forecast DB is stale; automatic publication repair refused" >&2
    exit 1
  fi
  if ! disk_output="$(df -P "$BASE_DIR" 2>&1)"; then
    echo "scheduler health blocked: disk usage check failed: $disk_output" >&2
    exit 1
  fi
  disk_field="$(printf '%s\n' "$disk_output" | awk 'NR == 2 { print $5 }')"
  if [[ ! "$disk_field" =~ ^[0-9]+%$ ]]; then
    echo "scheduler health blocked: disk usage output malformed" >&2
    exit 1
  fi
  disk_percent="${disk_field%%%}"
  if (( disk_percent >= DISK_MAX_PERCENT )); then
    echo "scheduler health blocked: disk usage ${disk_percent}% reached threshold ${DISK_MAX_PERCENT}%" >&2
    exit 1
  fi

  if [[ "$PUBLISH_PAGES" == "1" && -z "$PUBLIC_MANIFEST_URL" ]]; then
    echo "scheduler health blocked: public manifest URL is required when Pages publishing is enabled" >&2
    exit 1
  fi

  if ! local_structure="$(
    publication_validate validate \
      --artifact-root "$FORECASTER_DIR" \
      --manifest "$MANIFEST" \
      --require-strategy 2>&1
  )"; then
    echo "scheduler health blocked: local checksum or manifest validation failed: $local_structure" >&2
    exit 1
  fi

  public_tmp=""
  cleanup_public_tmp() {
    [[ -z "$public_tmp" ]] || rm -f "$public_tmp"
  }
  trap cleanup_public_tmp EXIT
  if [[ -n "$PUBLIC_MANIFEST_URL" ]]; then
    public_tmp="$(mktemp "${TMPDIR:-/tmp}/weatheredge-scheduler-public.XXXXXX")"
    public_poll_url="$PUBLIC_MANIFEST_URL"
    if [[ "$PUBLIC_MANIFEST_URL" == http://* || "$PUBLIC_MANIFEST_URL" == https://* ]]; then
      separator="?"
      [[ "$PUBLIC_MANIFEST_URL" == *"?"* ]] && separator="&"
      public_poll_url="${PUBLIC_MANIFEST_URL}${separator}scheduler=$(date +%s%N)"
    fi
    if ! curl -fsS -m 15 --retry 2 --retry-delay 2 --retry-all-errors \
      "$public_poll_url" >"$public_tmp"; then
      echo "scheduler health blocked: public manifest is unavailable" >&2
      exit 1
    fi
    chmod 0600 "$public_tmp"
    if ! public_structure="$(
      publication_validate validate-metadata \
        --manifest "$public_tmp" \
        --require-strategy 2>&1
    )"; then
      echo "scheduler health blocked: public manifest checksum or structure validation failed: $public_structure" >&2
      exit 1
    fi
  fi

  if ! (
    cd "$TRADING_DIR"
    run_as_app "$PYTHON_BIN" - "$MANIFEST" "$BUILD_INFO" "$public_tmp" <<'PY'
import json
import re
import sys

manifest_path, build_info_path, public_path = sys.argv[1:]
with open(manifest_path, encoding="utf-8") as handle:
    local = json.load(handle)
with open(build_info_path, encoding="utf-8") as handle:
    build = json.load(handle)

local_sha = local.get("provenance", {}).get("source_sha")
build_sha = build.get("source_sha")
valid = isinstance(build_sha, str) and re.fullmatch(r"[0-9a-f]{7,40}", build_sha)
if (
    not valid
    or build.get("source_dirty") is not False
    or local_sha != build_sha
):
    raise SystemExit(1)
if public_path:
    with open(public_path, encoding="utf-8") as handle:
        public = json.load(handle)
    if public.get("provenance", {}).get("source_sha") != build_sha:
        raise SystemExit(1)
PY
  ); then
    echo "scheduler health blocked: publication provenance mismatch; automatic repair refused" >&2
    exit 1
  fi

  local_operational_stale=0
  local_strategy_stale=0
  public_operational_stale=0
  public_strategy_stale=0
  if ! publication_validate validate \
    --artifact-root "$FORECASTER_DIR" \
    --manifest "$MANIFEST" \
    --require-strategy \
    --max-operational-age-minutes "$OPERATIONAL_MAX_MINUTES" >/dev/null 2>&1; then
    local_operational_stale=1
  fi
  if ! publication_validate validate \
    --artifact-root "$FORECASTER_DIR" \
    --manifest "$MANIFEST" \
    --require-strategy \
    --max-strategy-age-minutes "$STRATEGY_MAX_MINUTES" >/dev/null 2>&1; then
    local_strategy_stale=1
  fi
  if [[ -n "$public_tmp" ]]; then
    if ! publication_validate validate-metadata \
      --manifest "$public_tmp" \
      --require-strategy \
      --max-operational-age-minutes "$PUBLIC_OPERATIONAL_MAX_MINUTES" >/dev/null 2>&1; then
      public_operational_stale=1
    fi
    if ! publication_validate validate-metadata \
      --manifest "$public_tmp" \
      --require-strategy \
      --max-strategy-age-minutes "$STRATEGY_MAX_MINUTES" >/dev/null 2>&1; then
      public_strategy_stale=1
    fi
  fi

  printf 'local_operational_stale=%s\n' "$local_operational_stale"
  printf 'local_strategy_stale=%s\n' "$local_strategy_stale"
  printf 'public_operational_stale=%s\n' "$public_operational_stale"
  printf 'public_strategy_stale=%s\n' "$public_strategy_stale"
)

if [[ "$INTERNAL_MODE" == "__validate_publication_under_app_lock" ]]; then
  if (( EUID == 0 )); then
    echo "scheduler health blocked: internal artifact validation must run as the application user" >&2
    exit 1
  fi
  validate_configuration
  validate_publication_state
  exit 0
fi
if [[ -n "$INTERNAL_MODE" ]]; then
  echo "scheduler health blocked: unsupported internal mode" >&2
  exit 1
fi

if [[ -e "$DEPLOY_MAINTENANCE_MARKER" ]]; then
  echo "OK: deployment maintenance active; scheduler health check skipped"
  exit 0
fi

if ! /bin/bash "$INTEGRITY_HELPER"; then
  echo "scheduler health blocked: canonical systemd unit integrity failed" >&2
  exit 1
fi

timer_failures=0
for timer in "${CANONICAL_TIMERS[@]}"; do
  if ! "${SYSTEMCTL[@]}" is-enabled --quiet "$timer"; then
    echo "timer is not enabled: $timer" >&2
    timer_failures=$((timer_failures + 1))
  fi
  if ! "${SYSTEMCTL[@]}" is-active --quiet "$timer"; then
    echo "timer is not active: $timer" >&2
    timer_failures=$((timer_failures + 1))
  fi
done
if (( timer_failures > 0 )); then
  echo "scheduler health failed with $timer_failures timer state error(s)" >&2
  exit 1
fi

validate_configuration
if ! command -v "$FLOCK_BIN" >/dev/null 2>&1; then
  echo "scheduler health blocked: flock is required" >&2
  exit 1
fi
if (( EUID == 0 )); then
  if ! app_uid="$(/usr/bin/id -u "$APP_USER" 2>/dev/null)"; then
    echo "scheduler health blocked: application user does not exist" >&2
    exit 1
  fi
  if [[ "$app_uid" == "0" ]]; then
    echo "scheduler health blocked: application user must be unprivileged" >&2
    exit 1
  fi
fi

# A oneshot scan that fails or hangs leaves its timer "active", so the timer
# loop above kept passing while every scan failed from Oct 27 to Nov 1 (audit
# F5). Require each configured paper profile, a separate paper account, to have
# journaled a decision inside the window instead. Per profile, because one can
# fail while the other keeps scanning and the unit's single Result cannot say
# which; by age, so a failed tick or two does not fail health but a hang does. Rows
# from before a deploy quiesce or a reboot only count as stale once the scan
# timer has been active for a full window. The read runs as the application
# user (root must never create the WAL side files) and outside the artifact
# lock, and fails before any publication repair, like a timer error.
if [[ -n "$PAPER_DB" ]]; then
  if ! scan_timer_active_since="$(
    "${SYSTEMCTL[@]}" show --property=ActiveEnterTimestampMonotonic --value \
      sfo-kalshi-paper-scan.timer
  )"; then
    echo "scheduler health blocked: paper scan timer state is unreadable" >&2
    exit 1
  fi
  scan_status=0
  (
    cd "$TRADING_DIR"
    run_as_app "$PYTHON_BIN" - "$PAPER_DB" "$SCAN_PROFILES" \
      "$SCAN_MAX_AGE_MINUTES" "$scan_timer_active_since" <<'PY'
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sfo_kalshi_quant.config import normalize_risk_profile_name

db_path, profiles_csv, max_minutes, timer_active_since = sys.argv[1:]
now = datetime.now(timezone.utc)
window = timedelta(minutes=float(max_minutes))
expected = []
for raw in profiles_csv.split(","):
    if not raw.strip():
        continue
    try:
        profile = normalize_risk_profile_name(raw)
    except ValueError:
        print(f"invalid paper risk profile: {raw.strip()}", file=sys.stderr)
        raise SystemExit(2)
    if profile not in expected:
        expected.append(profile)
if not expected:
    print("no paper risk profile is configured", file=sys.stderr)
    raise SystemExit(2)


def parse_utc(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def profiles_with_recent_decisions():
    found = set()
    if not Path(db_path).is_file():
        return found
    conn = sqlite3.connect(
        Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30
    )
    try:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='decision_snapshots'"
        ).fetchone():
            return found
        # Walk back from the newest row and stop at the window edge, so a
        # profile that stopped long ago never reads the whole journal.
        for created_at, profile in conn.execute(
            "SELECT created_at, risk_profile FROM decision_snapshots "
            "ORDER BY id DESC"
        ):
            moment = parse_utc(created_at)
            if moment is None:
                continue
            if now - moment > window:
                break
            found.add(str(profile or "").strip().lower())
            if found.issuperset(expected):
                break
    finally:
        conn.close()
    return found


try:
    recent = profiles_with_recent_decisions()
except sqlite3.Error as exc:
    print(f"paper decision journal is unreadable: {exc}", file=sys.stderr)
    raise SystemExit(2)
stale = [profile for profile in expected if profile not in recent]
if not stale:
    raise SystemExit(0)

# systemd stamps unit transitions in CLOCK_MONOTONIC microseconds; 0 or an
# unreadable value means no grace.
try:
    activated_us = int(timer_active_since)
except ValueError:
    activated_us = 0
if activated_us > 0:
    active_for = timedelta(
        microseconds=time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000 - activated_us
    )
    if timedelta(0) <= active_for < window:
        print(
            "paper scan freshness deferred: sfo-kalshi-paper-scan.timer has been "
            f"active for {active_for.total_seconds() / 60:.1f} of {max_minutes} "
            f"minutes (start-up grace); no decision yet from {', '.join(stale)}"
        )
        raise SystemExit(0)
for profile in stale:
    print(
        f"paper scan profile={profile} recorded no decision in the last "
        f"{max_minutes} minutes",
        file=sys.stderr,
    )
raise SystemExit(3)
PY
  ) || scan_status=$?
  if (( scan_status == 3 )); then
    echo "scheduler health failed: paper scans stopped recording decisions" >&2
    exit 1
  fi
  if (( scan_status != 0 )); then
    echo "scheduler health blocked: paper scan freshness check failed (status=$scan_status)" >&2
    exit 1
  fi
fi

run_as_app /bin/mkdir -p "$(/usr/bin/dirname "$ARTIFACT_LOCK")"
validation_status=0
validation_output="$(
  run_as_app "$FLOCK_BIN" \
    -E 75 \
    -w "$VALIDATION_LOCK_WAIT_SECONDS" \
    "$ARTIFACT_LOCK" \
    /bin/bash "$0" __validate_publication_under_app_lock 2>&1
)" || validation_status=$?
if (( validation_status == 75 )); then
  echo "scheduler health blocked: artifact validation lock is busy after ${VALIDATION_LOCK_WAIT_SECONDS}s" >&2
  [[ -z "$validation_output" ]] || printf '%s\n' "$validation_output" >&2
  exit 1
fi
if (( validation_status != 0 )); then
  echo "scheduler health blocked: app-privileged artifact validation failed (status=$validation_status)" >&2
  [[ -z "$validation_output" ]] || printf '%s\n' "$validation_output" >&2
  exit 1
fi

local_operational_stale=""
local_strategy_stale=""
public_operational_stale=""
public_strategy_stale=""
validation_field_count=0
while IFS='=' read -r field value; do
  if [[ "$value" != "0" && "$value" != "1" ]]; then
    echo "scheduler health blocked: artifact validation result is malformed" >&2
    exit 1
  fi
  case "$field" in
    local_operational_stale)
      [[ -z "$local_operational_stale" ]] || {
        echo "scheduler health blocked: artifact validation result is malformed" >&2
        exit 1
      }
      local_operational_stale="$value"
      ;;
    local_strategy_stale)
      [[ -z "$local_strategy_stale" ]] || {
        echo "scheduler health blocked: artifact validation result is malformed" >&2
        exit 1
      }
      local_strategy_stale="$value"
      ;;
    public_operational_stale)
      [[ -z "$public_operational_stale" ]] || {
        echo "scheduler health blocked: artifact validation result is malformed" >&2
        exit 1
      }
      public_operational_stale="$value"
      ;;
    public_strategy_stale)
      [[ -z "$public_strategy_stale" ]] || {
        echo "scheduler health blocked: artifact validation result is malformed" >&2
        exit 1
      }
      public_strategy_stale="$value"
      ;;
    *)
      echo "scheduler health blocked: artifact validation result is malformed" >&2
      exit 1
      ;;
  esac
  validation_field_count=$((validation_field_count + 1))
done <<<"$validation_output"
if (( validation_field_count != 4 )); then
  echo "scheduler health blocked: artifact validation result is incomplete" >&2
  exit 1
fi

if (( local_operational_stale || local_strategy_stale || public_operational_stale || public_strategy_stale )); then
  mkdir -p "$REPAIR_STATE_DIR"
  chmod 0750 "$REPAIR_STATE_DIR"
  exec 9>"$REPAIR_STATE_DIR/repair.lock"
  if ! "$FLOCK_BIN" -n 9; then
    echo "OK: scheduler publication repair is already in progress"
    exit 0
  fi
  if [[ -e "$DEPLOY_MAINTENANCE_MARKER" ]]; then
    echo "OK: deployment maintenance began; scheduler repair skipped"
    exit 0
  fi

  repair_epoch="$(date +%s)"
  repair_stamp="$REPAIR_STATE_DIR/last-repair-epoch"
  if [[ -f "$repair_stamp" ]]; then
    previous_repair="$(<"$repair_stamp")"
    if [[ "$previous_repair" =~ ^[0-9]+$ ]] \
      && (( repair_epoch - previous_repair < REPAIR_COOLDOWN_SECONDS )); then
      # A repair already ran and published; remaining staleness here is that
      # publish still propagating. The repair path owns the alerting decision
      # via the consecutive-miss counter, so re-reporting it from inside the
      # cooldown only multiplies one incident into several failed runs.
      echo "OK: scheduler health remains stale inside the repair cooldown; awaiting the previous repair"
      exit 0
    fi
  fi
  repair_stamp_tmp="$REPAIR_STATE_DIR/.last-repair-epoch.$$"
  printf '%s\n' "$repair_epoch" >"$repair_stamp_tmp"
  mv -f "$repair_stamp_tmp" "$repair_stamp"

  if (( local_strategy_stale == 1 )); then
    if ! "${SYSTEMCTL[@]}" start sfo-strategy-lab-refresh.service; then
      echo "scheduler repair failed: Strategy Lab refresh failed" >&2
      exit 1
    fi
  fi
  if ! "${SYSTEMCTL[@]}" start sfo-operational-publish.service; then
    echo "scheduler repair failed: operational publication failed" >&2
    exit 1
  fi
  if ! run_as_app /usr/bin/env \
    "SFO_PUBLICATION_PROPAGATION_TIMEOUT_SECONDS=$PROPAGATION_TIMEOUT_SECONDS" \
    /bin/bash "$PROPAGATION_WAITER"; then
    propagation_misses=$(($(read_propagation_misses) + 1))
    record_propagation_misses "$propagation_misses"
    if (( propagation_misses >= PROPAGATION_FAILURE_THRESHOLD )); then
      echo "scheduler repair failed: public propagation did not converge across $propagation_misses consecutive repair(s)" >&2
      exit 1
    fi
    echo "OK: public propagation has not converged yet (miss ${propagation_misses}/${PROPAGATION_FAILURE_THRESHOLD}); the publish succeeded and the next cycle will re-check"
    exit 0
  fi
  clear_propagation_misses
  if ! run_as_app /bin/bash "$FINAL_HEALTH_CHECK"; then
    echo "scheduler repair failed: final forecast/publication health check failed" >&2
    exit 1
  fi
  echo "OK: scheduler repaired bounded publication freshness"
  exit 0
fi

if ! run_as_app /bin/bash "$FINAL_HEALTH_CHECK"; then
  echo "scheduler health blocked: final forecast/publication health check failed" >&2
  exit 1
fi

clear_propagation_misses
echo "OK: scheduler and publication health verified"
