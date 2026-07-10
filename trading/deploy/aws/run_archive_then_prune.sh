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

# 5. Only now may retention delete anything.
$PY -m sfo_kalshi_quant.cli --no-color --db-path "$DB" \
  paper-prune --full-days "${SFO_PRUNE_FULL_DAYS:-1}" --dedup-days "${SFO_PRUNE_DEDUP_DAYS:-45}"

# 6. Ring buffer: drop local copies >keep-days old ONLY if verifiably uploaded.
$PY -m sfo_kalshi_quant.cli --no-color --db-path "$DB" \
  paper-archive --archive-dir "$ARCHIVE_DIR" --cleanup --keep-days "${SFO_ARCHIVE_KEEP_DAYS:-30}" \
  || echo "WARN: ring-buffer cleanup failed" >&2
