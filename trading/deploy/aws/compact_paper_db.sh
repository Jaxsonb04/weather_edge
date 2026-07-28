#!/usr/bin/env bash
# Reclaim the free pages the retention prune leaves behind (2026-07-27 audit F.8).
#
# Deleting rows does not shrink a SQLite file; it moves pages onto the freelist.
# The production journal runs auto_vacuum=NONE, so PRAGMA incremental_vacuum is
# unavailable and a full rewrite is the only way to return space to the
# filesystem -- which the deploy's own backup preflight
# (database_bytes * 2 + 1 GiB) needs before it will let any deploy proceed.
#
# Deliberately NOT part of the nightly archive-then-prune chain: that unit runs
# under TimeoutStartSec=1800 which the chain already nearly exhausts, and a
# multi-minute exclusive-lock rewrite inside it would guarantee the timeout this
# work exists to eliminate. Operator-run, with the paper timers stopped.
#
# Uses VACUUM INTO rather than in-place VACUUM: the original stays untouched and
# readable while the compacted copy is built and verified, so a failure at any
# point before the swap leaves production exactly as it was.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TRADING_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
DB="${SFO_KALSHI_DB:-$TRADING_DIR/data/paper_trading.db}"
# Reclaiming less than this is not worth an exclusive-lock rewrite.
MIN_FREE_PAGES="${SFO_COMPACT_MIN_FREE_PAGES:-50000}"

for unit in \
  sfo-kalshi-paper-scan.timer \
  sfo-kalshi-paper-scan.service \
  sfo-kalshi-paper-monitor.timer \
  sfo-kalshi-paper-monitor.service \
  sfo-kalshi-paper-settle.timer \
  sfo-kalshi-paper-settle.service \
  sfo-kalshi-paper-prune.timer \
  sfo-kalshi-paper-prune.service; do
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$unit"; then
    echo "$unit is active; stop the paper timers before compacting" >&2
    exit 1
  fi
done

if [[ ! -f "$DB" ]]; then
  echo "paper database not found: $DB" >&2
  exit 1
fi

# Fold the WAL back into the main file first, so the compacted copy reflects
# every committed page and the stale sidecars can be retired with the old file.
sqlite3 "$DB" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null

page_size=$(sqlite3 -readonly "$DB" "PRAGMA page_size;")
page_count=$(sqlite3 -readonly "$DB" "PRAGMA page_count;")
free_pages=$(sqlite3 -readonly "$DB" "PRAGMA freelist_count;")
db_bytes=$(stat -c %s "$DB" 2>/dev/null || stat -f %z "$DB")
echo "page_size=$page_size page_count=$page_count freelist=$free_pages bytes=$db_bytes"

if (( free_pages < MIN_FREE_PAGES )); then
  echo "freelist $free_pages < $MIN_FREE_PAGES; nothing worth reclaiming"
  exit 0
fi

# VACUUM INTO writes a copy sized at roughly (page_count - freelist) pages. Ask
# for that plus 1 GiB of headroom so a mis-estimate cannot fill the volume.
est_bytes=$(( (page_count - free_pages) * page_size + 1073741824 ))
free_kb=$(df -Pk "$(dirname "$DB")" | awk 'NR == 2 {print $4}')
avail_bytes=$(( free_kb * 1024 ))
echo "estimated_copy_plus_headroom=$est_bytes available=$avail_bytes"
if (( avail_bytes < est_bytes )); then
  echo "insufficient free space to build the compacted copy" >&2
  exit 1
fi

TARGET="$DB.compact.$$"
PREVIOUS="$DB.pre-compact"
rm -f "$TARGET"
# Any failure before the swap must leave production untouched.
trap 'rm -f "$TARGET"' EXIT

echo "building compacted copy: $TARGET"
started=$(date +%s)
sqlite3 "$DB" "VACUUM INTO '$TARGET';"
echo "VACUUM INTO completed in $(( $(date +%s) - started ))s"

echo "verifying compacted copy"
integrity=$(sqlite3 -readonly "$TARGET" "PRAGMA integrity_check;")
if [[ "$integrity" != "ok" ]]; then
  echo "compacted copy failed integrity_check: $integrity" >&2
  exit 1
fi
foreign=$(sqlite3 -readonly "$TARGET" "PRAGMA foreign_key_check;" | head -5)
if [[ -n "$foreign" ]]; then
  echo "compacted copy has foreign key violations: $foreign" >&2
  exit 1
fi

# Row-count parity across every table. VACUUM must be content-preserving; a
# mismatch means we are about to swap in a database that lost data.
mismatch=0
while read -r table; do
  before=$(sqlite3 -readonly "$DB" "SELECT COUNT(*) FROM \"$table\";")
  after=$(sqlite3 -readonly "$TARGET" "SELECT COUNT(*) FROM \"$table\";")
  if [[ "$before" != "$after" ]]; then
    echo "ROW COUNT MISMATCH $table: before=$before after=$after" >&2
    mismatch=1
  fi
done < <(sqlite3 -readonly "$DB" "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;")
if (( mismatch != 0 )); then
  echo "refusing to swap: compacted copy is not row-count identical" >&2
  exit 1
fi
echo "row-count parity verified across all tables"

new_bytes=$(stat -c %s "$TARGET" 2>/dev/null || stat -f %z "$TARGET")
echo "compacted $db_bytes -> $new_bytes bytes (reclaimed $(( db_bytes - new_bytes )))"

# Swap. From here the trap must not delete the file we are installing.
trap - EXIT
mv "$DB" "$PREVIOUS"
mv "$TARGET" "$DB"
# The old sidecars describe the old file and MUST NOT survive alongside the new
# one; a stale -wal against a replaced main database is a corruption path.
rm -f "$PREVIOUS-wal" "$PREVIOUS-shm" "$DB-wal" "$DB-shm"

# VACUUM INTO emits a rollback-journal database. Production runs WAL, and the
# scanners assume it; restore the mode before any writer returns.
mode=$(sqlite3 "$DB" "PRAGMA journal_mode=WAL;")
echo "journal_mode now: $mode"
if [[ "$mode" != "wal" ]]; then
  echo "failed to restore WAL journal mode" >&2
  exit 1
fi

live_check=$(sqlite3 "$DB" "PRAGMA quick_check;")
if [[ "$live_check" != "ok" ]]; then
  echo "swapped-in database failed quick_check: $live_check" >&2
  exit 1
fi

chown --reference="$PREVIOUS" "$DB" 2>/dev/null || true
chmod --reference="$PREVIOUS" "$DB" 2>/dev/null || true

echo "compaction complete; previous file retained at $PREVIOUS"
echo "remove it once a full scan cycle has run clean:"
echo "  rm -f $PREVIOUS"
df -h "$(dirname "$DB")" | tail -1
