#!/usr/bin/env bash
set -euo pipefail

# Full operator-driven deploy: the sole production source-change path copies
# both source trees without deleting unrelated remote files. The manual
# recovery sync shares the same runtime-state exclusions.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEATHEREDGE_ROOT="${WEATHEREDGE_ROOT:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
ENV_FILE="${WEATHEREDGE_ENV_FILE:-$WEATHEREDGE_ROOT/.local/ec2.env}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

HOST_IP="${EC2_IP:-${LIGHTSAIL_IP:-}}"
HOST_KEY="${EC2_KEY:-${LIGHTSAIL_KEY:-}}"

if [[ -z "$HOST_IP" ]]; then
  echo "Set EC2_IP (or the legacy LIGHTSAIL_IP fallback) in $ENV_FILE or the environment." >&2
  exit 1
fi
if [[ -z "$HOST_KEY" ]]; then
  echo "Set EC2_KEY (or the legacy LIGHTSAIL_KEY fallback) in $ENV_FILE or the environment." >&2
  exit 1
fi
if [[ ! -f "$HOST_KEY" ]]; then
  echo "SSH key not found: $HOST_KEY" >&2
  exit 1
fi

REMOTE_USER="${REMOTE_USER:-ubuntu}"
REMOTE_BASE="${REMOTE_BASE:-/opt/weatheredge}"
LOCAL_TRADING_DIR="${LOCAL_TRADING_DIR:-$WEATHEREDGE_ROOT/trading}"
LOCAL_FORECASTER_DIR="${LOCAL_FORECASTER_DIR:-$WEATHEREDGE_ROOT/forecaster}"
FORECASTER_EXCLUDES="$SCRIPT_DIR/forecaster-runtime.rsync-filter"
QUIESCE_HELPER="$SCRIPT_DIR/disable_systemd_timers.sh"
BACKUP_HELPER="$SCRIPT_DIR/backup_paper_db.sh"
SYSTEMD_VERIFY_HELPER="$SCRIPT_DIR/verify_systemd_unit_integrity.sh"

# Audit F-07: this script deliberately needs NO local interpreter. It used to
# stamp build provenance by importing two package constants, and discovering
# Xcode's Python 3.9 at that point -- roughly 90 lines after production timers
# were already quiesced -- stranded the box mid-deploy with every writer
# stopped. Those constants are now read literally from source further down, so
# there is nothing left to resolve and nothing left to fail on.
DEPLOY_MAINTENANCE_MARKER="/run/weatheredge-deploy-maintenance"
SSH_OPTS=(
  -i "$HOST_KEY"
  -o StrictHostKeyChecking=accept-new
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=6
)

if [[ ! "$REMOTE_BASE" =~ ^/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$ ]]; then
  echo "REMOTE_BASE must be a canonical conservative absolute path: $REMOTE_BASE" >&2
  exit 1
fi
IFS='/' read -r -a REMOTE_BASE_COMPONENTS <<<"${REMOTE_BASE#/}"
for component in "${REMOTE_BASE_COMPONENTS[@]}"; do
  if [[ "$component" == "." || "$component" == ".." ]]; then
    echo "REMOTE_BASE must not contain '.' or '..' path components: $REMOTE_BASE" >&2
    exit 1
  fi
done

if [[ ! -f "$LOCAL_FORECASTER_DIR/google_weather_cache.py" ]]; then
  echo "Forecaster source not found: $LOCAL_FORECASTER_DIR" >&2
  exit 1
fi
if [[ ! -d "$LOCAL_TRADING_DIR/sfo_kalshi_quant" ]]; then
  echo "Trading source not found: $LOCAL_TRADING_DIR" >&2
  exit 1
fi
if [[ ! -f "$WEATHEREDGE_ROOT/pyproject.toml" || ! -f "$WEATHEREDGE_ROOT/README.md" ]]; then
  echo "Root Python project not found: $WEATHEREDGE_ROOT" >&2
  exit 1
fi
if [[ ! -f "$WEATHEREDGE_ROOT/requirements/production.lock" ]]; then
  echo "Hashed production dependency lock is missing." >&2
  exit 1
fi
if [[ ! -f "$FORECASTER_EXCLUDES" ]]; then
  echo "Rsync exclude manifest not found: $FORECASTER_EXCLUDES" >&2
  exit 1
fi
if [[ ! -f "$QUIESCE_HELPER" ]]; then
  echo "Systemd quiescence helper not found: $QUIESCE_HELPER" >&2
  exit 1
fi
if [[ ! -f "$BACKUP_HELPER" ]]; then
  echo "Database backup helper not found: $BACKUP_HELPER" >&2
  exit 1
fi
if [[ ! -f "$SYSTEMD_VERIFY_HELPER" ]]; then
  echo "Systemd unit verification helper not found: $SYSTEMD_VERIFY_HELPER" >&2
  exit 1
fi

chmod 600 "$HOST_KEY"

SOURCE_SHA="$(git -C "$WEATHEREDGE_ROOT" rev-parse HEAD)"
SOURCE_BRANCH="$(git -C "$WEATHEREDGE_ROOT" branch --show-current)"
if [[ "$SOURCE_BRANCH" != "main" ]]; then
  echo "Deploy requires clean main; current branch is $SOURCE_BRANCH." >&2
  exit 1
fi
if ! git -C "$WEATHEREDGE_ROOT" diff --quiet \
  || ! git -C "$WEATHEREDGE_ROOT" diff --cached --quiet \
  || [[ -n "$(git -C "$WEATHEREDGE_ROOT" ls-files --others --exclude-standard)" ]]; then
  echo "Deploy requires an exact clean commit; source_dirty would be true." >&2
  exit 1
fi

verify_deploy_source_unchanged() {
  local phase="$1"
  # Backup verification can run for hours while another task edits this shared
  # checkout. Never install those edits with the revision captured before it.
  if [[ "$(git -C "$WEATHEREDGE_ROOT" rev-parse HEAD)" != "$SOURCE_SHA" \
     || "$(git -C "$WEATHEREDGE_ROOT" branch --show-current)" != "main" ]] \
    || ! git -C "$WEATHEREDGE_ROOT" diff --quiet \
    || ! git -C "$WEATHEREDGE_ROOT" diff --cached --quiet \
    || [[ -n "$(git -C "$WEATHEREDGE_ROOT" ls-files --others --exclude-standard)" ]]; then
    echo "Deploy source changed during $phase; refusing to stamp or resume this deployment. Runtime remains quiesced." >&2
    exit 1
  fi
}

# Prove the database, AWS identity, and encrypted/versioned backup target are
# usable before stopping a single service. The same audited local helper is
# streamed for preflight and backup so an old remote source tree cannot weaken
# the deployment gate.
REMOTE_DB="$REMOTE_BASE/trading/data/paper_trading.db"
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  bash -s preflight "$REMOTE_DB" < "$BACKUP_HELPER"

# Preserve an intentional pause if the independent scheduler watchdog already
# exists, but enable it on the first deploy that introduces the unit.
SCHEDULER_WATCHDOG_WAS_ABSENT=0
scheduler_probe_status=0
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  bash -s probe sfo-scheduler-health.timer < "$QUIESCE_HELPER" \
  || scheduler_probe_status=$?
case "$scheduler_probe_status" in
  0) ;;
  10) SCHEDULER_WATCHDOG_WAS_ABSENT=1 ;;
  *)
    echo "failed to inspect scheduler watchdog before quiescence (status=$scheduler_probe_status)" >&2
    exit "$scheduler_probe_status"
    ;;
esac

# Preserve an intentional pause once either Apple unit exists, but enable each
# timer on the first deploy that introduces it. Otherwise the timerless install
# would create a disabled unit that the canonical scheduler check immediately
# reports as missing from the established host's captured policy.
APPLE_REFRESH_WAS_ABSENT=0
apple_refresh_probe_status=0
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  bash -s probe weatheredge-apple-refresh.timer < "$QUIESCE_HELPER" \
  || apple_refresh_probe_status=$?
case "$apple_refresh_probe_status" in
  0) ;;
  10) APPLE_REFRESH_WAS_ABSENT=1 ;;
  *)
    echo "failed to inspect Apple refresh timer before quiescence (status=$apple_refresh_probe_status)" >&2
    exit "$apple_refresh_probe_status"
    ;;
esac

APPLE_PURGE_WAS_ABSENT=0
apple_purge_probe_status=0
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  bash -s probe weatheredge-apple-purge.timer < "$QUIESCE_HELPER" \
  || apple_purge_probe_status=$?
case "$apple_purge_probe_status" in
  0) ;;
  10) APPLE_PURGE_WAS_ABSENT=1 ;;
  *)
    echo "failed to inspect Apple purge timer before quiescence (status=$apple_purge_probe_status)" >&2
    exit "$apple_purge_probe_status"
    ;;
esac

# Capture the established host's timer policy before quiescing it. Stream the
# current helper because the remote source tree may be older than this deploy.
# A failed transfer or install deliberately leaves the box quiesced; only a
# completely successful deploy restores the exact set that was enabled before.
enabled_timer_output="$(
  ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" bash -s capture < "$QUIESCE_HELPER"
)"
ENABLED_TIMERS=()
while IFS= read -r timer; do
  [[ -n "$timer" ]] && ENABLED_TIMERS+=("$timer")
done <<<"$enabled_timer_output"
if (( SCHEDULER_WATCHDOG_WAS_ABSENT == 1 )); then
  ENABLED_TIMERS+=("sfo-scheduler-health.timer")
fi
if (( APPLE_REFRESH_WAS_ABSENT == 1 )); then
  ENABLED_TIMERS+=("weatheredge-apple-refresh.timer")
fi
if (( APPLE_PURGE_WAS_ABSENT == 1 )); then
  ENABLED_TIMERS+=("weatheredge-apple-purge.timer")
fi

ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "sudo install -o root -g root -m 600 /dev/null '$DEPLOY_MAINTENANCE_MARKER'"
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" bash -s quiesce < "$QUIESCE_HELPER"

backup_output="$(
  ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
    bash -s backup "$REMOTE_DB" < "$BACKUP_HELPER"
)"
printf '%s\n' "$backup_output"
ANALYSIS_DB_SNAPSHOT="$(
  sed -n 's/^WEATHEREDGE_BACKUP_SNAPSHOT=//p' <<<"$backup_output" | tail -n 1
)"
if [[ ! "$ANALYSIS_DB_SNAPSHOT" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
  echo "verified backup did not return a conservative absolute snapshot path" >&2
  exit 1
fi
IFS='/' read -r -a ANALYSIS_DB_COMPONENTS <<<"${ANALYSIS_DB_SNAPSHOT#/}"
for component in "${ANALYSIS_DB_COMPONENTS[@]}"; do
  if [[ "$component" == "." || "$component" == ".." ]]; then
    echo "verified backup snapshot must not contain '.' or '..' path components" >&2
    exit 1
  fi
done

verify_deploy_source_unchanged "backup verification"

ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "sudo mkdir -p '$REMOTE_BASE/requirements' && sudo chown '$REMOTE_USER:$REMOTE_USER' '$REMOTE_BASE' '$REMOTE_BASE/requirements'"

# The sole Python manifest lives at the repository root and reads README.md
# while discovering the package below trading/. Send those build inputs before
# either installer runs; the package source itself is synced in the next rsync.
rsync -av \
  -e "ssh -i '$HOST_KEY' -o StrictHostKeyChecking=accept-new" \
  -- \
  "$WEATHEREDGE_ROOT/pyproject.toml" \
  "$WEATHEREDGE_ROOT/README.md" \
  "$REMOTE_USER@$HOST_IP:$REMOTE_BASE/"

rsync -av \
  -e "ssh -i '$HOST_KEY' -o StrictHostKeyChecking=accept-new" \
  -- \
  "$WEATHEREDGE_ROOT/requirements/production.lock" \
  "$REMOTE_USER@$HOST_IP:$REMOTE_BASE/requirements/production.lock"

rsync -av \
  -e "ssh -i '$HOST_KEY' -o StrictHostKeyChecking=accept-new" \
  --exclude-from="$FORECASTER_EXCLUDES" \
  "$LOCAL_FORECASTER_DIR/" \
  "$REMOTE_USER@$HOST_IP:$REMOTE_BASE/forecaster/"

rsync -av \
  -e "ssh -i '$HOST_KEY' -o StrictHostKeyChecking=accept-new" \
  --exclude '.git' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '.DS_Store' \
  --exclude '.env' \
  --exclude '*.p8' \
  --exclude '*.pem' \
  --exclude '*.key' \
  --exclude '.venv' \
  --exclude '.venv-dev' \
  --exclude 'venv' \
  --exclude '*.egg-info' \
  --exclude 'data' \
  --exclude 'tmp_*' \
  "$LOCAL_TRADING_DIR/" \
  "$REMOTE_USER@$HOST_IP:$REMOTE_BASE/trading/"

# Full sync intentionally avoids broad --delete semantics because production
# runtime state shares these trees. Remove only the audited source paths retired
# by TP-12/FC-7, and only after every transfer above has succeeded.
REMOTE_RETIRED_PATHS=(
  "$REMOTE_BASE/trading/pyproject.toml"
  "$REMOTE_BASE/trading/sfo_kalshi_quant/sfo-dataset-backfill.service.in"
  "$REMOTE_BASE/trading/sfo_kalshi_quant/sfo-forecaster-refresh.service.in"
  "$REMOTE_BASE/forecaster/forecast_tomorrow.py"
  "$REMOTE_BASE/forecaster/load_to_db.py"
  "$REMOTE_BASE/forecaster/combine_psv.py"
  "$REMOTE_BASE/forecaster/eda.py"
  "$REMOTE_BASE/forecaster/lstm_model.py"
  "$REMOTE_BASE/forecaster/xgboost_model.py"
  "$REMOTE_BASE/forecaster/ab_test.py"
  "$REMOTE_BASE/forecaster/compare_models.py"
  "$REMOTE_BASE/forecaster/features.py"
  "$REMOTE_BASE/forecaster/forecast_validation.py"
  "$REMOTE_BASE/forecaster/fetch_inland_history.py"
)
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" rm -f -- "${REMOTE_RETIRED_PATHS[@]}"

verify_deploy_source_unchanged "source transfer"

# Audit PR-01: immutable build provenance. The host tree is an rsync copy with
# no .git, so the deployed source revision must be stamped at sync time; the
# publication manifest and the Pages commit message carry it onward so the
# public site can identify the exact source that generated its artifacts.
BUILD_INFO_TMP="$(mktemp)"
SOURCE_DIRTY=false
# Audit F-07: read the version constants literally instead of importing them.
# Importing required a local interpreter, and `python3` resolves to the Xcode
# system build (3.9) on macOS, which fails on `from datetime import UTC` and
# aborted the deploy *after* the box had already been quiesced. Both constants
# are module-level string literals, so a literal read needs no interpreter at
# all and cannot be broken by an unrelated import error elsewhere in the
# package. `test_deploy_provenance_versions_match_imported_constants` asserts
# these stay identical to the imported values, so a format change is caught.
read_source_version_constant() {
  local source_file="$1"
  local constant_name="$2"
  local value
  value="$(
    sed -n "s/^${constant_name} = \"\([^\"]*\)\"\$/\1/p" "$source_file" \
      | head -n 1
  )"
  if [[ -z "$value" ]]; then
    echo "could not read $constant_name from $source_file" >&2
    exit 1
  fi
  printf '%s' "$value"
}

EXECUTION_MODEL_VERSION="$(
  read_source_version_constant \
    "$WEATHEREDGE_ROOT/trading/sfo_kalshi_quant/maker_fills.py" \
    EXECUTION_MODEL_VERSION
)"
ACCOUNTING_POLICY_VERSION="$(
  read_source_version_constant \
    "$WEATHEREDGE_ROOT/trading/sfo_kalshi_quant/account.py" \
    ACCOUNTING_POLICY_VERSION
)"
cat > "$BUILD_INFO_TMP" <<JSON
{
  "source_sha": "$SOURCE_SHA",
  "source_dirty": $SOURCE_DIRTY,
  "synced_at_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "execution_model_version": "$EXECUTION_MODEL_VERSION",
  "accounting_policy_version": "$ACCOUNTING_POLICY_VERSION"
}
JSON
rsync -av \
  -e "ssh -i '$HOST_KEY' -o StrictHostKeyChecking=accept-new" \
  -- \
  "$BUILD_INFO_TMP" \
  "$REMOTE_USER@$HOST_IP:$REMOTE_BASE/forecaster/build_info.json"
rm -f "$BUILD_INFO_TMP"

# Render the transferred units and refresh the editable Python installation
# while every timer remains stopped. The timer-less installer is the deployment
# gate: any dependency, package, or unit failure exits here and leaves the host
# safely quiesced instead of restarting a partial tree.
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "cd '$REMOTE_BASE/trading' && bash deploy/aws/install_systemd_notimers.sh"
# This read-only gate catches runtime drop-ins and other effective-unit drift
# after daemon-reload. Any mismatch exits while all producer timers are still
# quiesced; operators must remove the drift explicitly before retrying.
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "cd '$REMOTE_BASE/trading' && bash deploy/aws/verify_systemd_unit_integrity.sh"
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "cd '$REMOTE_BASE/trading' && bash deploy/aws/create_decision_snapshot_index.sh"
# Retention indexes share that quiesced window. They must exist before the prune
# timer is restored: without them the bounded-subquery prune degenerates to a
# correlated full scan per candidate row and is slower than what it replaced.
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "cd '$REMOTE_BASE/trading' && bash deploy/aws/create_retention_indexes.sh"
# Initialize the restart-era account schema while every producer is quiesced.
# The Strategy builder is intentionally read-only, so this gate must run before
# any timer restoration or seed publication.
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "cd '$REMOTE_BASE/trading' && .venv/bin/python deploy/aws/validate_account_cutover.py --db '$REMOTE_DB'"

# From this point onward the transferred source and account cutover have passed
# their gates and producer restoration may begin. Any later exit must either
# restore the exact captured timer policy and release maintenance, or quiesce
# the host again while retaining the marker. This prevents a split-brain state
# where timers run but the independent scheduler watchdog remains suppressed.
RUNTIME_RECOVERY_REQUIRED=1
# Recovery can run during historical analysis, before the normal timer split
# below. Resolve the watchdog now so set -u cannot abort that recovery, while
# preserving an operator's deliberately disabled watchdog.
SCHEDULER_WATCHDOG_ENABLED=0
for timer in ${ENABLED_TIMERS[@]+"${ENABLED_TIMERS[@]}"}; do
  if [[ "$timer" == "sfo-scheduler-health.timer" ]]; then
    SCHEDULER_WATCHDOG_ENABLED=1
  fi
done
recover_deploy_runtime() {
  local interrupted_status="${1:-$?}"
  local restore_status=0
  local release_status=0
  trap - EXIT HUP INT TERM

  if (( RUNTIME_RECOVERY_REQUIRED == 1 )); then
    if (( ${#ENABLED_TIMERS[@]} > 0 )); then
      if ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
        bash -s restore "${ENABLED_TIMERS[@]}" < "$QUIESCE_HELPER"; then
        :
      else
        restore_status=$?
      fi
    fi
    if (( restore_status == 0 )); then
      if ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
        "sudo rm -f -- '$DEPLOY_MAINTENANCE_MARKER'"; then
        RUNTIME_RECOVERY_REQUIRED=0
        if (( SCHEDULER_WATCHDOG_ENABLED == 1 )); then
          ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
            "sudo systemctl start sfo-scheduler-health.service" \
            || echo "warning: scheduler health recovery run failed; its timer remains active" >&2
        fi
      else
        release_status=$?
      fi
    fi
    if (( RUNTIME_RECOVERY_REQUIRED == 1 )); then
      ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
        bash -s quiesce < "$QUIESCE_HELPER" \
        || echo "warning: failed to re-quiesce after deployment recovery failure" >&2
      if (( restore_status != 0 )); then
        echo "warning: failed to restore captured timers during deployment recovery (status=$restore_status)" >&2
      fi
      if (( release_status != 0 )); then
        echo "warning: failed to release deployment maintenance during recovery (status=$release_status)" >&2
      fi
    fi
  fi
  exit "$interrupted_status"
}
trap 'recover_deploy_runtime 129' HUP
trap 'recover_deploy_runtime 130' INT
trap 'recover_deploy_runtime 143' TERM
trap 'recover_deploy_runtime $?' EXIT

# Historical analysis is diagnostic and the frequent builder has an explicit
# deferred state. Run it while deployment maintenance still holds every
# producer: the verified snapshot may temporarily push the runtime volume above
# its normal disk-health threshold, so it must be consumed and removed before
# any producer or freshness check is restored. The helper reads the immutable,
# integrity-checked deploy snapshot instead of the live journal.
ANALYSIS_CACHE_REFRESHED=0
if ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "cd '$REMOTE_BASE/trading' && SFO_STRATEGY_ANALYSIS_DB_PATH='$ANALYSIS_DB_SNAPSHOT' bash deploy/aws/refresh_strategy_analysis_cache.sh"; then
  ANALYSIS_CACHE_REFRESHED=1
else
  echo "warning: historical Strategy Lab cache refresh failed; continuing with deferred analysis" >&2
  # The helper uses a stable transient-unit name and normally cleans it up via
  # its trap. This best-effort second guard covers an abruptly dropped SSH
  # transport before the remote shell can run that trap.
  ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
    "sudo systemctl stop weatheredge-strategy-analysis-cache.service >/dev/null 2>&1 || true; sudo systemctl reset-failed weatheredge-strategy-analysis-cache.service >/dev/null 2>&1 || true" \
    || echo "warning: could not confirm Strategy Lab analysis unit cleanup" >&2
fi

# The analysis refresh was the last consumer of the verified snapshot. Drop it
# before restoring runtime health checks. It is redundant because this deploy
# already round-tripped it through S3 and re-verified the download.
if [[ -n "$ANALYSIS_DB_SNAPSHOT" ]]; then
  ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
    "rm -f -- '$ANALYSIS_DB_SNAPSHOT'" \
    || echo "warning: could not remove verified local snapshot $ANALYSIS_DB_SNAPSHOT" >&2
fi

# Restore producers first, seed and validate one complete publication, then
# restore the persistent watchdog last so it cannot race the first fresh build.
PRODUCER_TIMERS=()
WATCHDOG_ENABLED=0
SCHEDULER_WATCHDOG_ENABLED=0
PUBLISH_TIMER_ENABLED=0
STRATEGY_TIMER_ENABLED=0
for timer in ${ENABLED_TIMERS[@]+"${ENABLED_TIMERS[@]}"}; do
  if [[ "$timer" == "sfo-scheduler-health.timer" ]]; then
    SCHEDULER_WATCHDOG_ENABLED=1
  elif [[ "$timer" == "sfo-forecast-freshness.timer" ]]; then
    WATCHDOG_ENABLED=1
  elif [[ "$timer" == "sfo-operational-publish.timer" ]]; then
    # Keep the recurring publisher stopped until the one deploy-seed snapshot
    # is visible publicly. Otherwise the five-minute timer changes the local
    # manifest while the propagation waiter is checking the prior snapshot.
    PUBLISH_TIMER_ENABLED=1
  elif [[ "$timer" == "sfo-strategy-lab-refresh.timer" ]]; then
    # Strategy cycles also rebuild the global manifest. Hold this timer with
    # the publisher until the explicit seed has propagated.
    STRATEGY_TIMER_ENABLED=1
  else
    PRODUCER_TIMERS+=("$timer")
  fi
done
if (( ${#PRODUCER_TIMERS[@]} > 0 )); then
  ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
    bash -s restore "${PRODUCER_TIMERS[@]}" < "$QUIESCE_HELPER"
fi
INITIAL_HELD_TIMERS=()
if (( STRATEGY_TIMER_ENABLED == 1 )); then
  INITIAL_HELD_TIMERS+=("sfo-strategy-lab-refresh.timer")
fi
if (( PUBLISH_TIMER_ENABLED == 1 )); then
  INITIAL_HELD_TIMERS+=("sfo-operational-publish.timer")
fi
INITIAL_RESTORE_REQUIRED=1
restore_initial_timers() {
  local include_watchdog="${1:-0}"
  local restore_status=0
  local timers=()
  local timer=""
  for timer in ${INITIAL_HELD_TIMERS[@]+"${INITIAL_HELD_TIMERS[@]}"}; do
    timers+=("$timer")
  done
  if (( include_watchdog == 1 && WATCHDOG_ENABLED == 1 )); then
    timers+=("sfo-forecast-freshness.timer")
  fi
  if (( include_watchdog == 1 && SCHEDULER_WATCHDOG_ENABLED == 1 )); then
    timers+=("sfo-scheduler-health.timer")
  fi
  if (( INITIAL_RESTORE_REQUIRED == 1 )); then
    if (( ${#timers[@]} == 0 )); then
      INITIAL_RESTORE_REQUIRED=0
    elif ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
      bash -s restore "${timers[@]}" < "$QUIESCE_HELPER"; then
      INITIAL_RESTORE_REQUIRED=0
    else
      restore_status=$?
    fi
  fi
  return "$restore_status"
}
INITIAL_SEED_STATUS=0
if ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "sudo systemctl start sfo-strategy-lab-refresh.service && sudo systemctl start sfo-operational-publish.service"; then
  if ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
    "cd '$REMOTE_BASE/trading' && bash deploy/aws/wait_for_publication_manifest.sh"; then
    :
  else
    INITIAL_SEED_STATUS=$?
  fi
else
  INITIAL_SEED_STATUS=$?
fi
INITIAL_RESTART_STATUS=0
if restore_initial_timers "$(( INITIAL_SEED_STATUS != 0 ))"; then
  :
else
  INITIAL_RESTART_STATUS=$?
fi
if (( INITIAL_SEED_STATUS != 0 )); then
  echo "initial Strategy Lab publication failed (status=$INITIAL_SEED_STATUS)" >&2
fi
if (( INITIAL_RESTART_STATUS != 0 )); then
  echo "failed to restore held deployment timers after the initial publication (status=$INITIAL_RESTART_STATUS)" >&2
fi
if (( INITIAL_SEED_STATUS != 0 )); then
  exit "$INITIAL_SEED_STATUS"
fi
if (( INITIAL_RESTART_STATUS != 0 )); then
  exit "$INITIAL_RESTART_STATUS"
fi
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "sudo systemctl start sfo-forecast-freshness.service"
if (( WATCHDOG_ENABLED == 1 )); then
  ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
    bash -s restore sfo-forecast-freshness.timer < "$QUIESCE_HELPER"
fi

# A successful full analysis only updates the private cache. Rebuild the
# bounded public artifact from that cache, publish it, and wait for the exact
# immutable manifest captured by the waiter so the deploy does not finish with
# a fresh cache but a deferred public Strategy Lab.
if (( ANALYSIS_CACHE_REFRESHED == 1 )); then
  POST_ANALYSIS_TIMERS=()
  if (( STRATEGY_TIMER_ENABLED == 1 )); then
    POST_ANALYSIS_TIMERS+=("sfo-strategy-lab-refresh.timer")
  fi
  if (( PUBLISH_TIMER_ENABLED == 1 )); then
    POST_ANALYSIS_TIMERS+=("sfo-operational-publish.timer")
  fi
  POST_ANALYSIS_RESTORE_REQUIRED=0
  restore_post_analysis_timers() {
    local restore_status=0
    if (( POST_ANALYSIS_RESTORE_REQUIRED == 1 )) \
      && (( ${#POST_ANALYSIS_TIMERS[@]} > 0 )); then
      if ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
        bash -s restore "${POST_ANALYSIS_TIMERS[@]}" < "$QUIESCE_HELPER"; then
        POST_ANALYSIS_RESTORE_REQUIRED=0
      else
        restore_status=$?
      fi
    fi
    return "$restore_status"
  }
  POST_ANALYSIS_STATUS=0
  if (( ${#POST_ANALYSIS_TIMERS[@]} > 0 )); then
    # Prevent either recurring writer from racing cache promotion, the exact
    # Strategy rebuild, or its publication. Let already-running units finish
    # instead of terminating a Python write or git push midway through.
    POST_ANALYSIS_RESTORE_REQUIRED=1
    if ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
      "sudo systemctl stop sfo-strategy-lab-refresh.timer sfo-operational-publish.timer && timeout 910 bash -c 'while systemctl is-active --quiet sfo-strategy-lab-refresh.service || systemctl is-active --quiet sfo-operational-publish.service; do sleep 1; done'"; then
      :
    else
      POST_ANALYSIS_STATUS=$?
    fi
  fi
  if (( POST_ANALYSIS_STATUS == 0 )); then
    if ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
      "sudo systemctl start sfo-strategy-lab-refresh.service && sudo systemctl start sfo-operational-publish.service"; then
      if ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
        "cd '$REMOTE_BASE/trading' && bash deploy/aws/wait_for_publication_manifest.sh"; then
        :
      else
        POST_ANALYSIS_STATUS=$?
      fi
    else
      POST_ANALYSIS_STATUS=$?
    fi
  fi
  POST_ANALYSIS_RESTART_STATUS=0
  if restore_post_analysis_timers; then
    :
  else
    POST_ANALYSIS_RESTART_STATUS=$?
  fi
  if (( POST_ANALYSIS_STATUS != 0 )); then
    echo "post-analysis Strategy Lab publication failed (status=$POST_ANALYSIS_STATUS)" >&2
  fi
  if (( POST_ANALYSIS_RESTART_STATUS != 0 )); then
    echo "failed to restore recurring Strategy Lab/publication timers after post-analysis publication (status=$POST_ANALYSIS_RESTART_STATUS)" >&2
  fi
  if (( POST_ANALYSIS_STATUS != 0 )); then
    exit "$POST_ANALYSIS_STATUS"
  fi
  if (( POST_ANALYSIS_RESTART_STATUS != 0 )); then
    exit "$POST_ANALYSIS_RESTART_STATUS"
  fi
fi

if (( SCHEDULER_WATCHDOG_ENABLED == 1 )); then
  ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
    bash -s restore sfo-scheduler-health.timer < "$QUIESCE_HELPER"
fi
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "sudo rm -f -- '$DEPLOY_MAINTENANCE_MARKER'"
RUNTIME_RECOVERY_REQUIRED=0
trap - EXIT HUP INT TERM
if (( SCHEDULER_WATCHDOG_ENABLED == 1 )); then
  ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
    "sudo systemctl start sfo-scheduler-health.service"
fi

echo "Synced root packaging inputs, forecaster, and trading source to $REMOTE_USER@$HOST_IP:$REMOTE_BASE"
echo "Local source: $WEATHEREDGE_ROOT"
echo "Restored ${#PRODUCER_TIMERS[@]} producer timer(s); watchdog restored last=$WATCHDOG_ENABLED."
echo "Scheduler watchdog restored after maintenance=$SCHEDULER_WATCHDOG_ENABLED."
echo "Historical Strategy Lab cache refreshed=$ANALYSIS_CACHE_REFRESHED."
