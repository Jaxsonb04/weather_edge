#!/usr/bin/env bash
# One-time maintenance for the decision-snapshot reporting index.
# Pause the scan and monitor timers before running so a large journal can build
# the index without contending with paper-book writers.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TRADING_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
DB="${SFO_KALSHI_DB:-$TRADING_DIR/data/paper_trading.db}"

for unit in \
  sfo-kalshi-paper-scan.timer \
  sfo-kalshi-paper-scan.service \
  sfo-kalshi-paper-monitor.timer \
  sfo-kalshi-paper-monitor.service; do
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$unit"; then
    echo "$unit is active; stop the paper-scan and paper-monitor timers first" >&2
    exit 1
  fi
done

if [[ ! -f "$DB" ]]; then
  echo "paper database not found: $DB" >&2
  exit 1
fi

db_bytes=$(stat -c %s "$DB" 2>/dev/null || stat -f %z "$DB")
free_kb=$(df -Pk "$(dirname "$DB")" | awk 'NR == 2 {print $4}')
if (( free_kb * 1024 < db_bytes )); then
  echo "insufficient free space to build the decision snapshot index safely" >&2
  exit 1
fi

python3 - "$DB" <<'PY'
import sqlite3
import sys

db = sys.argv[1]
with sqlite3.connect(db, timeout=60.0) as conn:
    conn.execute("PRAGMA busy_timeout = 60000")
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='decision_snapshots'"
    ).fetchone() is None:
        raise SystemExit("decision_snapshots table is missing")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_decision_snapshots_created_market
        ON decision_snapshots (created_at, market_ticker, approved)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_decision_snapshots_pre_entry
        ON decision_snapshots (
            target_date, market_ticker, side, approved DESC, created_at, id
        )
        WHERE COALESCE(intraday_is_complete, 0) = 0
          AND market_close_time IS NOT NULL
          AND created_at < market_close_time
        """
    )
    conn.execute("ANALYZE")
print(f"decision snapshot reporting index ready: {db}")
PY
