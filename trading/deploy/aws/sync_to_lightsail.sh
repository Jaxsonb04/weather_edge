#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${LIGHTSAIL_IP:-}" ]]; then
  echo "Set LIGHTSAIL_IP first, e.g. export LIGHTSAIL_IP=1.2.3.4" >&2
  exit 1
fi

if [[ -z "${LIGHTSAIL_KEY:-}" ]]; then
  echo "Set LIGHTSAIL_KEY first, e.g. export LIGHTSAIL_KEY=$HOME/Downloads/key.pem" >&2
  exit 1
fi

if [[ ! -f "$LIGHTSAIL_KEY" ]]; then
  echo "SSH key not found: $LIGHTSAIL_KEY" >&2
  exit 1
fi

REMOTE_USER="${REMOTE_USER:-ubuntu}"
REMOTE_BASE="${REMOTE_BASE:-/opt/weatheredge}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEATHEREDGE_ROOT="${WEATHEREDGE_ROOT:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
LOCAL_TRADING_DIR="${LOCAL_TRADING_DIR:-$WEATHEREDGE_ROOT/trading}"
LOCAL_FORECASTER_DIR="${LOCAL_FORECASTER_DIR:-$WEATHEREDGE_ROOT/forecaster}"
SSH_OPTS=(-i "$LIGHTSAIL_KEY" -o StrictHostKeyChecking=accept-new)

if [[ ! -f "$LOCAL_FORECASTER_DIR/google_weather_cache.py" ]]; then
  echo "Forecaster source not found: $LOCAL_FORECASTER_DIR" >&2
  exit 1
fi

if [[ ! -d "$LOCAL_TRADING_DIR/sfo_kalshi_quant" ]]; then
  echo "Trading source not found: $LOCAL_TRADING_DIR" >&2
  exit 1
fi

chmod 600 "$LIGHTSAIL_KEY"

ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$LIGHTSAIL_IP" \
  "sudo mkdir -p '$REMOTE_BASE' && sudo chown '$REMOTE_USER:$REMOTE_USER' '$REMOTE_BASE'"

rsync -av \
  -e "ssh -i '$LIGHTSAIL_KEY' -o StrictHostKeyChecking=accept-new" \
  --exclude '.git' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '.DS_Store' \
  --exclude '.env' \
  --exclude '.google_weather_usage.json' \
  --exclude '.venv' \
  --exclude 'venv' \
  --exclude 'logs' \
  --exclude 'weather.db' \
  --exclude '*.db-journal' \
  --exclude '*.sqlite' \
  --exclude '*.sqlite3' \
  --exclude 'google_weather_cache.json' \
  --exclude 'trading_signal.json' \
  --exclude 'strategy_research.json' \
  --exclude 'cities_data.json' \
  --exclude 'publication_manifest.json' \
  --exclude 'dataset_research.json' \
  --exclude 'tmp_*' \
  "$LOCAL_FORECASTER_DIR/" \
  "$REMOTE_USER@$LIGHTSAIL_IP:$REMOTE_BASE/forecaster/"

rsync -av \
  -e "ssh -i '$LIGHTSAIL_KEY' -o StrictHostKeyChecking=accept-new" \
  --exclude '.git' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '.DS_Store' \
  --exclude '.env' \
  --exclude '.venv' \
  --exclude 'venv' \
  --exclude 'data' \
  --exclude 'tmp_*' \
  "$LOCAL_TRADING_DIR/" \
  "$REMOTE_USER@$LIGHTSAIL_IP:$REMOTE_BASE/trading/"

echo "Synced forecaster and trading repos to $REMOTE_USER@$LIGHTSAIL_IP:$REMOTE_BASE"
echo "Local source: $WEATHEREDGE_ROOT"
echo "Next:"
echo "  ssh -i \"$LIGHTSAIL_KEY\" $REMOTE_USER@$LIGHTSAIL_IP"
echo "  cd $REMOTE_BASE/trading"
echo "  bash deploy/aws/install_systemd.sh"
