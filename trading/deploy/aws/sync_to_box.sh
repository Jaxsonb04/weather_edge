#!/usr/bin/env bash
set -euo pipefail

# Full operator-driven deploy: copy both source trees without deleting unrelated
# remote files. The scheduled source-only sync intentionally uses --delete for
# tracked forecaster source; both paths share the runtime-state exclusions.
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
SSH_OPTS=(-i "$HOST_KEY" -o StrictHostKeyChecking=accept-new)

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

# Prove the database, AWS identity, and encrypted/versioned backup target are
# usable before stopping a single service. The same audited local helper is
# streamed for preflight and backup so an old remote source tree cannot weaken
# the deployment gate.
REMOTE_DB="$REMOTE_BASE/trading/data/paper_trading.db"
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  bash -s preflight "$REMOTE_DB" < "$BACKUP_HELPER"

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

ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" bash -s quiesce < "$QUIESCE_HELPER"

ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  bash -s backup "$REMOTE_DB" < "$BACKUP_HELPER"

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
  --exclude '.venv' \
  --exclude '.venv-dev' \
  --exclude 'venv' \
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

# Audit PR-01: immutable build provenance. The host tree is an rsync copy with
# no .git, so the deployed source revision must be stamped at sync time; the
# publication manifest and the Pages commit message carry it onward so the
# public site can identify the exact source that generated its artifacts.
BUILD_INFO_TMP="$(mktemp)"
SOURCE_DIRTY=false
EXECUTION_MODEL_VERSION="$(PYTHONPATH="$WEATHEREDGE_ROOT/trading" python3 -c 'from sfo_kalshi_quant.maker_fills import EXECUTION_MODEL_VERSION; print(EXECUTION_MODEL_VERSION)')"
ACCOUNTING_POLICY_VERSION="$(PYTHONPATH="$WEATHEREDGE_ROOT/trading" python3 -c 'from sfo_kalshi_quant.account import ACCOUNTING_POLICY_VERSION; print(ACCOUNTING_POLICY_VERSION)')"
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

# Restore producers first, seed and validate one complete publication, then
# restore the persistent watchdog last so it cannot race the first fresh build.
PRODUCER_TIMERS=()
WATCHDOG_ENABLED=0
for timer in ${ENABLED_TIMERS[@]+"${ENABLED_TIMERS[@]}"}; do
  if [[ "$timer" == "sfo-forecast-freshness.timer" ]]; then
    WATCHDOG_ENABLED=1
  else
    PRODUCER_TIMERS+=("$timer")
  fi
done
if (( ${#PRODUCER_TIMERS[@]} > 0 )); then
  ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
    bash -s restore "${PRODUCER_TIMERS[@]}" < "$QUIESCE_HELPER"
fi
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "sudo systemctl start sfo-strategy-lab-refresh.service && sudo systemctl start sfo-operational-publish.service"
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "cd '$REMOTE_BASE/trading' && bash deploy/aws/wait_for_publication_manifest.sh"
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "sudo systemctl start sfo-forecast-freshness.service"
if (( WATCHDOG_ENABLED == 1 )); then
  ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
    bash -s restore sfo-forecast-freshness.timer < "$QUIESCE_HELPER"
fi

echo "Synced root packaging inputs, forecaster, and trading source to $REMOTE_USER@$HOST_IP:$REMOTE_BASE"
echo "Local source: $WEATHEREDGE_ROOT"
echo "Restored ${#PRODUCER_TIMERS[@]} producer timer(s); watchdog restored last=$WATCHDOG_ENABLED."
