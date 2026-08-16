#!/usr/bin/env bash
# Archive-gated retention maintenance for paper_trading.db.
#
# Scheduled runs default to archive-only: export, upload, archive gate, and FK
# audit still execute, while live-DB deletion stays off. A prune may ONLY run
# in the explicit quiesced-delete mode after every complete UTC day of every
# snapshot table is losslessly exported and verified (manifest-backed).
# Upload and feature-rollup failures are non-fatal: raw local archive files are
# the safety property; the 30-day ring buffer absorbs S3 outages, and features
# can always be rebuilt from the archive.
set -euo pipefail

TRADING_DIR="${SFO_TRADING_ROOT:-/opt/weatheredge/trading}"
PY="${SFO_TRADING_PYTHON:-$TRADING_DIR/.venv/bin/python}"
DB="${SFO_KALSHI_DB:-$TRADING_DIR/data/paper_trading.db}"
ARCHIVE_DIR="${SFO_ARCHIVE_DIR:-$TRADING_DIR/data/archive}"
PRUNE_MODE="${SFO_PRUNE_MODE:-archive-only}"
cd "$TRADING_DIR"

# 1. Lossless export of every unarchived complete UTC day (hard requirement).
"$PY" -m sfo_kalshi_quant.cli --no-color --db-path "$DB" \
  paper-archive --archive-dir "$ARCHIVE_DIR"

# 2. Feature rollup from the archive files (non-fatal; rebuildable anytime).
"$PY" -m sfo_kalshi_quant.cli --no-color --db-path "$DB" \
  paper-features --archive-dir "$ARCHIVE_DIR" \
  || echo "WARN: feature rollup failed; raw archive is intact" >&2

# 3. Push to S3 (non-fatal; skipped cleanly until SFO_ARCHIVE_S3_BUCKET is set).
"$PY" -m sfo_kalshi_quant.cli --no-color --db-path "$DB" \
  paper-archive --archive-dir "$ARCHIVE_DIR" --upload \
  || echo "WARN: S3 upload failed; local ring buffer retains files" >&2

# 4. Hard gate: refuses unless every complete UTC day is archived+verified.
"$PY" -m sfo_kalshi_quant.cli --no-color --db-path "$DB" \
  paper-archive --archive-dir "$ARCHIVE_DIR" --check-gate

# 5. Explicit integrity audit (kept out of normal PaperStore initialization).
"$PY" -m sfo_kalshi_quant.cli --no-color --db-path "$DB" \
  paper-check-foreign-keys --limit "${SFO_FK_AUDIT_LIMIT:-100}"

# 6. Live-DB deletion is temporarily safe-off on scheduled runs. The archive,
# upload, exact-coverage gate, and FK audit above still execute every night.
# `quiesced-delete` is an explicit operator assertion that all journal writers
# have been stopped; it retains the bounded low-level prune for future manual
# maintenance without putting that write-heavy path back on the default timer.
if [[ "$PRUNE_MODE" == "quiesced-delete" ]]; then
  echo "NOTICE: quiesced live-DB deletion explicitly enabled; paper writers must be stopped" >&2

  # Index precondition. The prune's dedup grouping and its parent-orphan probes
  # depend on the retention indexes; without them each probe becomes a
  # correlated full scan and the unit exhausts TimeoutStartSec.
  missing_indexes="$(
    "$PY" - "$DB" <<'PY'
import sqlite3
import sys

sys.path.insert(0, ".")
from sfo_kalshi_quant.store.schema import RETENTION_INDEX_NAMES

with sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True) as conn:
    present = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
print(",".join(name for name in RETENTION_INDEX_NAMES if name not in present))
PY
)" || missing_indexes=""
  if [[ -n "$missing_indexes" ]]; then
    echo "WARN: retention indexes missing ($missing_indexes); the prune will run" >&2
    echo "WARN: without index support and may exceed its start timeout. Pause the" >&2
    echo "WARN: paper timers and run deploy/aws/create_retention_indexes.sh." >&2
  fi

  "$PY" -m sfo_kalshi_quant.cli --no-color --db-path "$DB" \
    paper-prune --full-days "${SFO_PRUNE_FULL_DAYS:-1}" --dedup-days "${SFO_PRUNE_DEDUP_DAYS:-45}" \
    --batch-limit "${SFO_PRUNE_BATCH_LIMIT:-5000}" \
    --max-batch-seconds "${SFO_PRUNE_MAX_BATCH_SECONDS:-2}" \
    --batch-pause-seconds "${SFO_PRUNE_BATCH_PAUSE_SECONDS:-0.15}"
else
  if [[ "$PRUNE_MODE" != "archive-only" ]]; then
    echo "DEGRADED: unrecognized SFO_PRUNE_MODE=$PRUNE_MODE; failing closed to archive-only" >&2
  fi
  echo "DEGRADED: archive/upload/gate/FK complete; scheduled live-DB deletion skipped" >&2
  echo "DEGRADED: journal growth continues; disk watchdog remains the safety alarm" >&2
fi

# 7. Ring buffer: drop local copies >keep-days old ONLY if verifiably uploaded.
"$PY" -m sfo_kalshi_quant.cli --no-color --db-path "$DB" \
  paper-archive --archive-dir "$ARCHIVE_DIR" --cleanup --keep-days "${SFO_ARCHIVE_KEEP_DAYS:-30}" \
  || echo "WARN: ring-buffer cleanup failed" >&2
