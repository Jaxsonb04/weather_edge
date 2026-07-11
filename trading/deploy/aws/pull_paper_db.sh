#!/usr/bin/env bash
set -euo pipefail

# Pull the live paper-trading journal (decision_snapshots etc.) DOWN from the
# EC2 box so the backtest-rescore + real-money readiness gate can run locally
# against real settled data. sync_to_box.sh only pushes source
# OUT and deliberately excludes the DB, so this is the missing inbound half.
#
# Usage (from anywhere; defaults to .local/ec2.env):
#   EC2_IP=... EC2_KEY=/path/to/key.pem bash trading/deploy/aws/pull_paper_db.sh
#   bash trading/deploy/aws/pull_paper_db.sh
#   PYTHONPATH=trading python3 -m sfo_kalshi_quant.cli backtest-rescore --db-path trading/data/paper_trading.db

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
REMOTE_DB="${REMOTE_DB:-$REMOTE_BASE/trading/data/paper_trading.db}"
LOCAL_DB="${LOCAL_DB:-$WEATHEREDGE_ROOT/trading/data/paper_trading.db}"
SSH_OPTS=(-i "$HOST_KEY" -o StrictHostKeyChecking=accept-new)

chmod 600 "$HOST_KEY"
mkdir -p "$(dirname "$LOCAL_DB")"

# Back up any existing local DB so a pull never silently clobbers local state.
if [[ -f "$LOCAL_DB" ]]; then
  backup="$LOCAL_DB.local-backup"
  cp "$LOCAL_DB" "$backup"
  echo "Backed up existing local DB -> $backup"
fi

# Checkpoint the WAL on the box so the copied file holds all committed rows, then
# pull the main DB file (the -wal/-shm are transient and not needed once merged).
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" \
  "sqlite3 '$REMOTE_DB' 'PRAGMA wal_checkpoint(TRUNCATE);' >/dev/null 2>&1 || true"

rsync -av \
  -e "ssh -i '$HOST_KEY' -o StrictHostKeyChecking=accept-new" \
  "$REMOTE_USER@$HOST_IP:$REMOTE_DB" \
  "$LOCAL_DB"

echo "Pulled $REMOTE_USER@$HOST_IP:$REMOTE_DB -> $LOCAL_DB"
echo "Next: PYTHONPATH=trading python3 -m sfo_kalshi_quant.cli backtest-rescore --db-path \"$LOCAL_DB\""
