#!/usr/bin/env bash
# One-time maintenance for the nightly-retention indexes (2026-07-27 audit F.8).
#
# The prune's dedup grouping and its correlated parent-orphan probes have no
# index to stand on in an existing journal, which is why the nightly unit burned
# 21 minutes of wall clock for 5 minutes of CPU and died on TimeoutStartSec.
# Fresh databases build these at init; an existing journal gets them here, once,
# with the paper-book writers paused so a multi-minute build does not contend.
#
# Safe to re-run: every statement is CREATE INDEX IF NOT EXISTS and the script
# short-circuits when all definitions are already present.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TRADING_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
DB="${SFO_KALSHI_DB:-$TRADING_DIR/data/paper_trading.db}"

cd "$TRADING_DIR"
# shellcheck source=trading/deploy/aws/lib/resolve_python.sh
. "$SCRIPT_DIR/lib/resolve_python.sh"
if ! PY="$(weatheredge_resolve_python "${PYTHON:-}" "$TRADING_DIR/.venv/bin/python")"; then
  exit 1
fi

# Every unit that writes the paper book. An index build holds the write lock for
# minutes; letting a scanner interleave risks SQLITE_BUSY on the trading path,
# which is the one thing this maintenance must never cause.
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
    echo "$unit is active; stop the paper scan/monitor/settle/prune timers first" >&2
    exit 1
  fi
done

if [[ ! -f "$DB" ]]; then
  echo "paper database not found: $DB" >&2
  exit 1
fi

# Validate definitions BEFORE the free-space gate, so an already-correct journal
# is a cheap no-op rather than being blocked by a conservative disk check that
# only matters when we actually intend to build something.
index_state="$(
"$PY" - "$DB" <<'PY'
import sqlite3
import sys

sys.path.insert(0, ".")
from sfo_kalshi_quant.store.schema import RETENTION_INDEX_NAMES

db = sys.argv[1]
with sqlite3.connect(db, timeout=60.0) as conn:
    conn.execute("PRAGMA busy_timeout = 60000")
    present = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
missing = [name for name in RETENTION_INDEX_NAMES if name not in present]
print("current" if not missing else "needs-build:" + ",".join(missing))
PY
)"
if [[ "$index_state" == "current" ]]; then
  echo "retention indexes already current: $DB"
  exit 0
fi
echo "building ${index_state#needs-build:}"

# Same conservative one-database-copy gate the reporting-index builder uses.
# SQLite materialises a temporary B-tree while sorting each index.
db_bytes=$(stat -c %s "$DB" 2>/dev/null || stat -f %z "$DB")
free_kb=$(df -Pk "$(dirname "$DB")" | awk 'NR == 2 {print $4}')
if (( free_kb * 1024 < db_bytes )); then
  echo "insufficient free space to build the retention indexes safely" >&2
  exit 1
fi

"$PY" - "$DB" <<'PY'
import sqlite3
import sys
import time

sys.path.insert(0, ".")
from sfo_kalshi_quant.store.schema import (
    DECISION_SNAPSHOT_RETENTION_INDEXES,
    RETENTION_INDEX_NAMES,
)

db = sys.argv[1]
# Long timeout: these builds legitimately take minutes on a 10.9 GB journal, and
# a spurious SQLITE_BUSY here would leave the set half-built.
with sqlite3.connect(db, timeout=1800.0) as conn:
    conn.execute("PRAGMA busy_timeout = 1800000")
    # Spill sort runs to disk rather than into a 3.7 GB box's RAM; the unit that
    # runs the prune is capped at MemoryMax=1600M and an in-memory sort of a
    # multi-hundred-megabyte index is exactly how that cap gets hit.
    conn.execute("PRAGMA temp_store = FILE")
    for statement in filter(
        None, (s.strip() for s in DECISION_SNAPSHOT_RETENTION_INDEXES.split(";"))
    ):
        name = statement.split("IF NOT EXISTS", 1)[-1].split()[0]
        started = time.monotonic()
        conn.execute(statement)
        conn.commit()
        print(f"  {name}: {time.monotonic() - started:.1f}s", flush=True)
    # The planner cannot choose these without stats; an un-ANALYZEd new index is
    # a common reason a "fixed" query keeps its old plan.
    started = time.monotonic()
    conn.execute("ANALYZE")
    conn.commit()
    print(f"  ANALYZE: {time.monotonic() - started:.1f}s", flush=True)

    present = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
missing = [name for name in RETENTION_INDEX_NAMES if name not in present]
if missing:
    raise SystemExit(f"retention indexes still missing after build: {missing}")
print(f"retention indexes ready: {db}")
PY
