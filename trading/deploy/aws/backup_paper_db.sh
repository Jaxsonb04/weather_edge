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
# S3 is the durable 35-day rollback tier. Keep only a short local rollback
# window so multi-gigabyte snapshots do not consume the runtime volume.
KEEP_DAYS="${KEEP_DAYS:-1}"
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

# Reclaim aged local snapshots BEFORE measuring free space. This sweep used to
# run only at the very end of `backup` mode, which made the gate unsatisfiable
# by its own output: the snapshot a deploy leaves behind occupies exactly the
# space the next deploy's preflight demands, and the sweep that would reclaim it
# sits behind that failing check. Running it here -- in both modes, before the
# measurement -- means the check sees the space the box can actually offer.
if [[ -d "$BACKUP_DIR" ]]; then
  find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name 'paper_trading-*.sqlite3' -o -name 'paper_trading-*.sqlite3.sha256' \) \
    -mtime "+$KEEP_DAYS" -delete 2>/dev/null || true
fi

# A verified backup holds ONE copy at a time: the snapshot is deleted locally
# once S3 has it, before the restore copy is pulled back for verification. So
# the peak is a single database plus an operating margin, not two.
#
# This matters more than it looks. Both sides of the comparison move with the
# database -- growing it consumes free space AND raises the requirement -- so
# the old 2x constraint tightened three times as fast as the file grew, and on
# the live volume it yielded a hard ceiling of ~10.3 GB against a journal that
# grows ~690 MB/day. That was about one deployable day per compaction. At 1x
# the same volume allows ~15.4 GB.
database_bytes="$(wc -c < "$DB_PATH")"
available_kib="$(df -Pk "$(dirname "$DB_PATH")" | awk 'NR == 2 {print $4}')"
if [[ ! "$available_kib" =~ ^[0-9]+$ ]]; then
  echo "could not determine available database-volume capacity" >&2
  exit 1
fi
available_bytes=$((available_kib * 1024))
required_bytes=$((database_bytes + 1073741824))
if (( available_bytes < required_bytes )); then
  echo "database backup needs space for one snapshot + 1 GiB headroom" >&2
  echo "required=$required_bytes available=$available_bytes; clean only verified old local backups first" >&2
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

backup_phase() {
  # Keep command-substitution stdout reserved for the caller's result marker.
  # Deliberately omit paths, object names and digests from progress messages.
  printf 'database backup: %s\n' "$1" >&2
}

backup_phase "creating SQLite snapshot"
escaped_snapshot="${snapshot//\\/\\\\}"
escaped_snapshot="${escaped_snapshot//\"/\\\"}"
sqlite3 "$DB_PATH" ".backup \"$escaped_snapshot\""
chmod 600 "$snapshot"

# Hash the snapshot now, then run the expensive full SQLite checks once on the
# independently downloaded, checksum-identical copy. Checking the same bytes
# before upload adds no recovery proof and can double a large journal's pause.
# An unusable upload must never be promoted or reported as a verified backup.
backup_phase "hashing snapshot"
sha256sum "$snapshot" > "$checksum"
chmod 600 "$checksum"
expected_sha="$(awk '{print $1}' "$checksum")"

object_key="$PREFIX/$(basename "$snapshot")"
backup_phase "uploading snapshot and checksum"
"$AWS_CLI" s3 cp "$snapshot" "s3://$BUCKET/$object_key" \
  --sse AES256 --only-show-errors
"$AWS_CLI" s3 cp "$checksum" "s3://$BUCKET/$object_key.sha256" \
  --sse AES256 --only-show-errors
# Drop the local snapshot before pulling the restore copy: S3 now holds it, so
# a second simultaneous copy buys nothing and doubles the volume requirement.
# If anything below fails the script aborts having already removed it, which is
# safe -- the object is in S3 and the live database was never touched.
rm -f -- "$snapshot"

backup_phase "downloading restore copy"
"$AWS_CLI" s3 cp "s3://$BUCKET/$object_key" "$restore_copy" --only-show-errors

backup_phase "verifying restored checksum"
restored_sha="$(sha256sum "$restore_copy" | awk '{print $1}')"
if [[ "$restored_sha" != "$expected_sha" ]]; then
  echo "downloaded backup checksum mismatch" >&2
  exit 1
fi
backup_phase "checking restored SQLite integrity"
restored_integrity="$(sqlite3 -batch -noheader "$restore_copy" 'PRAGMA integrity_check;')"
if [[ "$restored_integrity" != "ok" ]]; then
  echo "downloaded backup failed integrity_check: $restored_integrity" >&2
  exit 1
fi
backup_phase "checking restored foreign keys"
if ! restored_foreign_keys="$(sqlite3 -batch -noheader "$restore_copy" 'PRAGMA foreign_key_check;')"; then
  echo "downloaded backup foreign_key_check command failed" >&2
  exit 1
fi
if [[ -n "$restored_foreign_keys" ]]; then
  echo "downloaded backup failed foreign_key_check" >&2
  exit 1
fi
backup_phase "verification complete"

# The sweep now runs before the free-space check above; repeating it here would
# be a no-op on the snapshot just written. The CALLER owns this snapshot's
# lifetime -- sync_to_box.sh still needs it to build the Strategy Lab analysis
# cache -- and deletes it once finished. It is safe to delete because this
# script has already round-tripped it through S3 and re-verified the download.

# Hand the caller the copy that provably survived the round trip, rather than
# the one that was merely uploaded. Same path as before, so callers are
# unaffected; the cleanup trap then finds an empty restore directory.
mv -f -- "$restore_copy" "$snapshot"
chmod 600 "$snapshot"

echo "verified off-host database backup: s3://$BUCKET/$object_key"
echo "WEATHEREDGE_BACKUP_SNAPSHOT=$snapshot"
