#!/usr/bin/env bash
# Archive-gated retention for paper_trading.db.
#
# Ordering contract: the prune may ONLY run after every complete UTC day of
# every snapshot table is losslessly exported and verified (manifest-backed).
# A failed archive aborts this script before the prune line is reached, and
# the explicit --check-gate is a second, independent guard.  Upload and
# feature-rollup failures are non-fatal: raw local archive files are the
# safety property; the 30-day ring buffer absorbs S3 outages, and features
# can always be rebuilt from the archive.
set -euo pipefail

cd /opt/weatheredge/trading
PY=.venv/bin/python
DB="${SFO_KALSHI_DB:-/opt/weatheredge/trading/data/paper_trading.db}"
ARCHIVE_DIR="${SFO_ARCHIVE_DIR:-/opt/weatheredge/trading/data/archive}"

# 1. Lossless export of every unarchived complete UTC day (hard requirement).
$PY -m sfo_kalshi_quant.cli --no-color --db-path "$DB" \
  paper-archive --archive-dir "$ARCHIVE_DIR"

# 2. Feature rollup from the archive files (non-fatal; rebuildable anytime).
$PY -m sfo_kalshi_quant.cli --no-color --db-path "$DB" \
  paper-features --archive-dir "$ARCHIVE_DIR" \
  || echo "WARN: feature rollup failed; raw archive is intact" >&2

# 3. Push to S3 (non-fatal; skipped cleanly until SFO_ARCHIVE_S3_BUCKET is set).
$PY -m sfo_kalshi_quant.cli --no-color --db-path "$DB" \
  paper-archive --archive-dir "$ARCHIVE_DIR" --upload \
  || echo "WARN: S3 upload failed; local ring buffer retains files" >&2

# 4. Hard gate: refuses unless every complete UTC day is archived+verified.
$PY -m sfo_kalshi_quant.cli --no-color --db-path "$DB" \
  paper-archive --archive-dir "$ARCHIVE_DIR" --check-gate

# 5. Explicit integrity audit (kept out of normal PaperStore initialization).
$PY -m sfo_kalshi_quant.cli --no-color --db-path "$DB" \
  paper-check-foreign-keys --limit "${SFO_FK_AUDIT_LIMIT:-100}"

# 5b. Index precondition. The prune's dedup grouping and its parent-orphan
# probes depend on the retention indexes; without them each probe becomes a
# correlated full scan and the unit exhausts TimeoutStartSec. Warn rather than
# abort: the batched prune commits per batch, so even a slow run leaves durable
# progress, whereas refusing to run leaves the journal growing unchecked.
missing_indexes="$(
  $PY - "$DB" <<'PY'
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

# 6. Only now may retention delete anything.
# Batch geometry is a LATENCY contract with the 24/7 scan (5 min) and monitor
# (2 min), not just a throughput knob: a batch that holds the write lock past
# their 30 s busy_timeout costs them a whole tick (audit F-02). Time-bounding
# the batch trades some total prune runtime for that guarantee; the unit's
# TimeoutStartSec=1800 has ample room over the measured 255 s.
$PY -m sfo_kalshi_quant.cli --no-color --db-path "$DB" \
  paper-prune --full-days "${SFO_PRUNE_FULL_DAYS:-1}" --dedup-days "${SFO_PRUNE_DEDUP_DAYS:-45}" \
  --batch-limit "${SFO_PRUNE_BATCH_LIMIT:-5000}" \
  --max-batch-seconds "${SFO_PRUNE_MAX_BATCH_SECONDS:-2}" \
  --batch-pause-seconds "${SFO_PRUNE_BATCH_PAUSE_SECONDS:-0.15}"

# 7. Ring buffer: drop local copies >keep-days old ONLY if verifiably uploaded.
$PY -m sfo_kalshi_quant.cli --no-color --db-path "$DB" \
  paper-archive --archive-dir "$ARCHIVE_DIR" --cleanup --keep-days "${SFO_ARCHIVE_KEEP_DAYS:-30}" \
  || echo "WARN: ring-buffer cleanup failed" >&2
