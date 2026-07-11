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
SSH_OPTS=(-i "$HOST_KEY" -o StrictHostKeyChecking=accept-new)

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

chmod 600 "$HOST_KEY"

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

echo "Synced root packaging inputs, forecaster, and trading source to $REMOTE_USER@$HOST_IP:$REMOTE_BASE"
echo "Local source: $WEATHEREDGE_ROOT"
echo "Next:"
echo "  ssh -i \"$HOST_KEY\" $REMOTE_USER@$HOST_IP"
echo "  cd $REMOTE_BASE/trading"
echo "  bash deploy/aws/install_systemd.sh"
