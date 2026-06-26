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

# Config-drift guard. This rsync runs WITHOUT --delete, so any uncommitted local
# edit that gets pushed lives on the box as code that exists in no git ref -- the
# "box runs code ahead of HEAD" drift the 2026-06-20 audit found (the live
# strategy_research.py emitted fields present in no commit). Refuse to deploy a
# dirty trading/forecaster tree (override with ALLOW_DIRTY_DEPLOY=1 for an
# emergency hotfix), and record the deployed commit on the box so git-vs-box drift
# is auditable afterward.
GIT_SHA="unknown"
if git -C "$WEATHEREDGE_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  GIT_SHA="$(git -C "$WEATHEREDGE_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  if [[ -n "$(git -C "$WEATHEREDGE_ROOT" status --porcelain -- trading forecaster 2>/dev/null)" ]]; then
    if [[ "${ALLOW_DIRTY_DEPLOY:-0}" != "1" ]]; then
      echo "Refusing to deploy: trading/ or forecaster/ has uncommitted changes." >&2
      echo "Commit them first so the box never runs code that exists in no git ref," >&2
      echo "or re-run with ALLOW_DIRTY_DEPLOY=1 to override (emergency hotfix only)." >&2
      git -C "$WEATHEREDGE_ROOT" status --short -- trading forecaster >&2
      exit 1
    fi
    echo "WARNING: deploying a DIRTY working tree (ALLOW_DIRTY_DEPLOY=1)." >&2
    GIT_SHA="$GIT_SHA-dirty"
  fi
fi
echo "Deploying git $GIT_SHA"

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
  --exclude 'strategy_research.protected.json' \
  --exclude 'dataset_research.json' \
  --exclude '/index.html' \
  --exclude '/details.html' \
  --exclude '/strategy-lab.html' \
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

# Record what was deployed so a future audit can compare the box against git
# (best-effort; never fails the deploy).
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$LIGHTSAIL_IP" \
  "printf '%s deployed %s\n' '$GIT_SHA' \"\$(date -u +%Y-%m-%dT%H:%M:%SZ)\" > '$REMOTE_BASE/trading/DEPLOYED_SHA.txt'" \
  || echo "WARNING: could not write DEPLOYED_SHA.txt to the box (non-fatal)." >&2

echo "Synced forecaster and trading repos to $REMOTE_USER@$LIGHTSAIL_IP:$REMOTE_BASE (git $GIT_SHA)"
echo "Local source: $WEATHEREDGE_ROOT"
echo "Next:"
echo "  ssh -i \"$LIGHTSAIL_KEY\" $REMOTE_USER@$LIGHTSAIL_IP"
echo "  cd $REMOTE_BASE/trading"
echo "  bash deploy/aws/install_systemd.sh"
