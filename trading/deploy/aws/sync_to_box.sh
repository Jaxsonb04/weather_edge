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
if [[ ! -f "$FORECASTER_EXCLUDES" ]]; then
  echo "Rsync exclude manifest not found: $FORECASTER_EXCLUDES" >&2
  exit 1
fi
if [[ ! -f "$QUIESCE_HELPER" ]]; then
  echo "Systemd quiescence helper not found: $QUIESCE_HELPER" >&2
  exit 1
fi

chmod 600 "$HOST_KEY"

# The remote may still contain an older source tree, so stream the current
# canonical helper over SSH instead of invoking a path that this sync has not
# transferred yet. A failed transfer deliberately leaves timers disabled and
# paired services inactive; the operator reinstalls/enables only after the
# complete tree is present and verified.
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" "bash -s" < "$QUIESCE_HELPER"

ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "sudo mkdir -p '$REMOTE_BASE' && sudo chown '$REMOTE_USER:$REMOTE_USER' '$REMOTE_BASE'"

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

echo "Synced root packaging inputs, forecaster, and trading source to $REMOTE_USER@$HOST_IP:$REMOTE_BASE"
echo "Local source: $WEATHEREDGE_ROOT"
echo "Next:"
echo "  ssh -i \"$HOST_KEY\" $REMOTE_USER@$HOST_IP"
echo "  cd $REMOTE_BASE/trading"
echo "  bash deploy/aws/install_systemd_notimers.sh"
echo "  Inspect /etc/weatheredge.env and run manual service checks against the synced tree."
echo "  After verification only: bash deploy/aws/install_systemd.sh"
