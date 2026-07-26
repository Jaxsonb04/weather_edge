#!/usr/bin/env bash
# One-time maintenance for decision-snapshot reporting and admission indexes.
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

# Most deploys are no-ops. Validate definitions before applying the conservative
# one-database-copy free-space gate so a growing journal does not block every
# later deploy merely because the already-correct indexes exist.
index_state="$(
python3 - "$DB" <<'PY'
import re
import sqlite3
import sys

db = sys.argv[1]
definitions = {
    "idx_decision_snapshots_created_market": """
        CREATE INDEX idx_decision_snapshots_created_market
        ON decision_snapshots (created_at, market_ticker, approved)
    """,
    "idx_decision_snapshots_pre_entry": """
        CREATE INDEX idx_decision_snapshots_pre_entry
        ON decision_snapshots (
            target_date, market_ticker, side, approved DESC, created_at, id
        )
        WHERE COALESCE(intraday_is_complete, 0) = 0
          AND market_close_time IS NOT NULL
          AND created_at < market_close_time
    """,
    "idx_decision_snapshots_pending_research_admission": """
        CREATE INDEX idx_decision_snapshots_pending_research_admission
        ON decision_snapshots (id)
        WHERE research_sleeve IS NOT NULL
          AND approved = 0
          AND entry_block_reason = 'research admission pending'
    """,
}

def normalized(sql: str | None) -> str:
    return re.sub(r"\s+", " ", str(sql or "").strip()).lower()

with sqlite3.connect(db, timeout=60.0) as conn:
    conn.execute("PRAGMA busy_timeout = 60000")
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='decision_snapshots'"
    ).fetchone() is None:
        raise SystemExit("decision_snapshots table is missing")
    present = {
        str(name): str(sql or "")
        for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='decision_snapshots'"
        )
    }
    current = all(
        normalized(present.get(name)) == normalized(definition)
        for name, definition in definitions.items()
    )
print("current" if current else "needs-build")
PY
)"
if [[ "$index_state" == "current" ]]; then
  echo "decision snapshot reporting and admission indexes already current: $DB"
  exit 0
fi

db_bytes=$(stat -c %s "$DB" 2>/dev/null || stat -f %z "$DB")
free_kb=$(df -Pk "$(dirname "$DB")" | awk 'NR == 2 {print $4}')
if (( free_kb * 1024 < db_bytes )); then
  echo "insufficient free space to build the decision snapshot index safely" >&2
  exit 1
fi

python3 - "$DB" <<'PY'
import re
import sqlite3
import sys

db = sys.argv[1]
definitions = {
    "idx_decision_snapshots_created_market": """
        CREATE INDEX idx_decision_snapshots_created_market
        ON decision_snapshots (created_at, market_ticker, approved)
    """,
    "idx_decision_snapshots_pre_entry": """
        CREATE INDEX idx_decision_snapshots_pre_entry
        ON decision_snapshots (
            target_date, market_ticker, side, approved DESC, created_at, id
        )
        WHERE COALESCE(intraday_is_complete, 0) = 0
          AND market_close_time IS NOT NULL
          AND created_at < market_close_time
    """,
    "idx_decision_snapshots_pending_research_admission": """
        CREATE INDEX idx_decision_snapshots_pending_research_admission
        ON decision_snapshots (id)
        WHERE research_sleeve IS NOT NULL
          AND approved = 0
          AND entry_block_reason = 'research admission pending'
    """,
}

def normalized(sql: str | None) -> str:
    return re.sub(r"\s+", " ", str(sql or "").strip()).lower()

with sqlite3.connect(db, timeout=60.0) as conn:
    conn.execute("PRAGMA busy_timeout = 60000")
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='decision_snapshots'"
    ).fetchone() is None:
        raise SystemExit("decision_snapshots table is missing")
    present = {
        str(name): str(sql or "")
        for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='decision_snapshots'"
        )
    }
    changed = False
    for name, definition in definitions.items():
        if normalized(present.get(name)) == normalized(definition):
            continue
        conn.execute(f'DROP INDEX IF EXISTS "{name}"')
        conn.execute(definition)
        changed = True
    if changed:
        conn.execute("ANALYZE decision_snapshots")
print(f"decision snapshot reporting and admission indexes ready: {db}")
PY
