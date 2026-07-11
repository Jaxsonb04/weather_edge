#!/usr/bin/env bash
set -euo pipefail

# Pull a transactionally consistent SQLite snapshot DOWN from EC2 for offline
# rescoring. sqlite3 .backup captures committed WAL state without disturbing
# live readers or checkpointing the production journal.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEATHEREDGE_ROOT="${WEATHEREDGE_ROOT:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
ENV_FILE="${WEATHEREDGE_ENV_FILE:-$WEATHEREDGE_ROOT/.local/ec2.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

HOST_IP="${EC2_IP:-${LIGHTSAIL_IP:-}}"
HOST_KEY="${EC2_KEY:-${LIGHTSAIL_KEY:-}}"
: "${HOST_IP:?Set EC2_IP (or the legacy LIGHTSAIL_IP fallback)}"
: "${HOST_KEY:?Set EC2_KEY (or the legacy LIGHTSAIL_KEY fallback)}"
[[ -f "$HOST_KEY" ]] || { echo "SSH key not found: $HOST_KEY" >&2; exit 1; }

REMOTE_USER="${REMOTE_USER:-ubuntu}"
REMOTE_BASE="${REMOTE_BASE:-/opt/weatheredge}"
REMOTE_DB="${REMOTE_DB:-$REMOTE_BASE/trading/data/paper_trading.db}"
REMOTE_TMP_DIR="${REMOTE_TMP_DIR:-/tmp}"
LOCAL_DB="${LOCAL_DB:-$WEATHEREDGE_ROOT/trading/data/paper_trading.db}"
SSH_OPTS=(-i "$HOST_KEY" -o StrictHostKeyChecking=accept-new)
REMOTE_TMP="${REMOTE_TMP_DIR%/}/weatheredge-paper-db.${REMOTE_USER}.$$.$RANDOM.sqlite3"
LOCAL_TMP=""
REMOTE_CREATED=0

remote_remove() {
  local command
  printf -v command 'rm -f -- %q %q %q' \
    "$REMOTE_TMP" "${REMOTE_TMP}-wal" "${REMOTE_TMP}-shm"
  ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" "$command" </dev/null
}

cleanup() {
  local status=$?
  trap - EXIT HUP INT TERM
  [[ -z "$LOCAL_TMP" ]] || rm -f -- \
    "$LOCAL_TMP" "${LOCAL_TMP}-wal" "${LOCAL_TMP}-shm"
  if [[ "$REMOTE_CREATED" -eq 1 ]]; then
    remote_remove || echo "WARN: failed to remove remote snapshot: $REMOTE_TMP" >&2
  fi
  exit "$status"
}
trap cleanup EXIT HUP INT TERM

chmod 600 "$HOST_KEY"
mkdir -p "$(dirname "$LOCAL_DB")"
LOCAL_TMP="$(mktemp "$(dirname "$LOCAL_DB")/.$(basename "$LOCAL_DB").pull.XXXXXX")"

remote_command=""
printf -v remote_command 'bash -s -- %q %q' "$REMOTE_DB" "$REMOTE_TMP"
REMOTE_CREATED=1
ssh "${SSH_OPTS[@]}" "$REMOTE_USER@$HOST_IP" "$remote_command" <<'REMOTE_SCRIPT'
set -euo pipefail
db_path="$1"
snapshot_path="$2"
escaped_snapshot="${snapshot_path//\\/\\\\}"
escaped_snapshot="${escaped_snapshot//\"/\\\"}"
sqlite3 "$db_path" ".backup \"$escaped_snapshot\""
integrity="$(sqlite3 -batch -noheader "$snapshot_path" 'PRAGMA integrity_check;')"
[[ "$integrity" == "ok" ]] || {
  echo "remote SQLite snapshot failed integrity_check: $integrity" >&2
  exit 1
}
REMOTE_SCRIPT

RSYNC_SSH=""
REMOTE_SOURCE=""
printf -v RSYNC_SSH 'ssh -i %q -o StrictHostKeyChecking=accept-new' "$HOST_KEY"
printf -v REMOTE_SOURCE '%s@%s:%q' "$REMOTE_USER" "$HOST_IP" "$REMOTE_TMP"
rsync -av \
  -e "$RSYNC_SSH" \
  "$REMOTE_SOURCE" "$LOCAL_TMP"

local_integrity="$(sqlite3 -batch -noheader "$LOCAL_TMP" 'PRAGMA integrity_check;')"
[[ "$local_integrity" == "ok" ]] || {
  echo "downloaded SQLite snapshot failed integrity_check: $local_integrity" >&2
  exit 1
}
# Opening a WAL-mode snapshot for verification can create empty temp-named
# sidecars. They are not part of the verified backup and must not be published.
rm -f -- "${LOCAL_TMP}-wal" "${LOCAL_TMP}-shm"

# Cleanup is part of success: preserve the old destination if remote cleanup
# fails, then atomically publish the verified local snapshot.
remote_remove
REMOTE_CREATED=0
mv -f -- "$LOCAL_TMP" "$LOCAL_DB"
LOCAL_TMP=""

echo "Pulled verified snapshot $REMOTE_USER@$HOST_IP:$REMOTE_DB -> $LOCAL_DB"
echo "Next: PYTHONPATH=trading python3 -m sfo_kalshi_quant.cli backtest-rescore --db-path \"$LOCAL_DB\""
