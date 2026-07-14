#!/usr/bin/env bash
set -euo pipefail

# Stream-safe deployment gate. `preflight` is read-only and must pass before
# timers are quiesced. `backup` snapshots the committed WAL state, uploads it,
# downloads it to a temporary restore path, and verifies both SQLite integrity
# and foreign keys before source or schema changes are allowed.

MODE="${1:-}"
DB_PATH="${2:-/opt/weatheredge/trading/data/paper_trading.db}"
ENV_FILE="${SFO_WEATHEREDGE_ENV_FILE:-/etc/weatheredge.env}"

case "$MODE" in
  preflight|backup) ;;
  *) echo "usage: $0 preflight|backup [database-path]" >&2; exit 2 ;;
esac

env_value() {
  local name="$1"
  local current="${!name:-}"
  if [[ -n "$current" ]]; then
    printf '%s' "$current"
    return
  fi
  if [[ -f "$ENV_FILE" ]]; then
    sudo awk -F= -v key="$name" '
      $1 == key {
        sub(/^[^=]*=/, "")
        sub(/\r$/, "")
        print
        exit
      }
    ' "$ENV_FILE"
  fi
}

BUCKET="$(env_value SFO_ARCHIVE_S3_BUCKET)"
PREFIX="$(env_value SFO_DATABASE_BACKUP_S3_PREFIX)"
AWS_CLI="$(env_value SFO_ARCHIVE_AWS_CLI)"
BACKUP_DIR="$(env_value SFO_DATABASE_BACKUP_DIR)"
KEEP_DAYS="$(env_value SFO_DATABASE_BACKUP_KEEP_DAYS)"
ALLOW_EMPTY="$(env_value SFO_ALLOW_EMPTY_DATABASE_DEPLOY)"

PREFIX="${PREFIX:-database-snapshots}"
PREFIX="${PREFIX#/}"
PREFIX="${PREFIX%/}"
AWS_CLI="${AWS_CLI:-aws}"
BACKUP_DIR="${BACKUP_DIR:-$(dirname "$DB_PATH")/backups}"
KEEP_DAYS="${KEEP_DAYS:-7}"
ALLOW_EMPTY="${ALLOW_EMPTY:-0}"

if [[ ! "$KEEP_DAYS" =~ ^[0-9]+$ ]]; then
  echo "SFO_DATABASE_BACKUP_KEEP_DAYS must be a non-negative integer" >&2
  exit 1
fi
if [[ -z "$BUCKET" ]]; then
  echo "SFO_ARCHIVE_S3_BUCKET is required for deployment backups" >&2
  exit 1
fi
if [[ ! -f "$DB_PATH" ]]; then
  if [[ "$ALLOW_EMPTY" == "1" ]]; then
    echo "database backup skipped for explicitly authorized empty host: $DB_PATH"
    exit 0
  fi
  echo "authoritative database is missing: $DB_PATH" >&2
  exit 1
fi

for command in sqlite3 sha256sum mktemp "$AWS_CLI"; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "required backup command is unavailable: $command" >&2
    exit 1
  fi
done

if ! "$AWS_CLI" sts get-caller-identity >/dev/null 2>&1; then
  echo "AWS identity is unavailable for the database backup gate" >&2
  exit 1
fi
if ! "$AWS_CLI" s3api get-bucket-location --bucket "$BUCKET" >/dev/null 2>&1; then
  echo "backup bucket is unavailable to the instance role: $BUCKET" >&2
  exit 1
fi

if [[ "$MODE" == "preflight" ]]; then
  echo "database backup preflight passed"
  exit 0
fi

umask 077
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
snapshot="$BACKUP_DIR/paper_trading-$timestamp.sqlite3"
checksum="$snapshot.sha256"
restore_dir="$(mktemp -d -- "$BACKUP_DIR/.restore-check.XXXXXX")"
restore_copy="$restore_dir/$(basename "$snapshot")"

cleanup() {
  local status=$?
  trap - EXIT HUP INT TERM
  rm -rf -- "$restore_dir"
  exit "$status"
}
trap cleanup EXIT HUP INT TERM

escaped_snapshot="${snapshot//\\/\\\\}"
escaped_snapshot="${escaped_snapshot//\"/\\\"}"
sqlite3 "$DB_PATH" ".backup \"$escaped_snapshot\""
chmod 600 "$snapshot"

integrity="$(sqlite3 -batch -noheader "$snapshot" 'PRAGMA integrity_check;')"
if [[ "$integrity" != "ok" ]]; then
  echo "database snapshot failed integrity_check: $integrity" >&2
  exit 1
fi
if [[ -n "$(sqlite3 -batch -noheader "$snapshot" 'PRAGMA foreign_key_check;')" ]]; then
  echo "database snapshot failed foreign_key_check" >&2
  exit 1
fi
sha256sum "$snapshot" > "$checksum"
chmod 600 "$checksum"
expected_sha="$(awk '{print $1}' "$checksum")"

object_key="$PREFIX/$(basename "$snapshot")"
"$AWS_CLI" s3 cp "$snapshot" "s3://$BUCKET/$object_key" \
  --sse AES256 --only-show-errors
"$AWS_CLI" s3 cp "$checksum" "s3://$BUCKET/$object_key.sha256" \
  --sse AES256 --only-show-errors
"$AWS_CLI" s3 cp "s3://$BUCKET/$object_key" "$restore_copy" --only-show-errors

restored_sha="$(sha256sum "$restore_copy" | awk '{print $1}')"
if [[ "$restored_sha" != "$expected_sha" ]]; then
  echo "downloaded backup checksum mismatch" >&2
  exit 1
fi
restored_integrity="$(sqlite3 -batch -noheader "$restore_copy" 'PRAGMA integrity_check;')"
if [[ "$restored_integrity" != "ok" ]]; then
  echo "downloaded backup failed integrity_check: $restored_integrity" >&2
  exit 1
fi
if [[ -n "$(sqlite3 -batch -noheader "$restore_copy" 'PRAGMA foreign_key_check;')" ]]; then
  echo "downloaded backup failed foreign_key_check" >&2
  exit 1
fi

find "$BACKUP_DIR" -maxdepth 1 -type f \
  \( -name 'paper_trading-*.sqlite3' -o -name 'paper_trading-*.sqlite3.sha256' \) \
  -mtime "+$KEEP_DAYS" -delete

echo "verified off-host database backup: s3://$BUCKET/$object_key"
