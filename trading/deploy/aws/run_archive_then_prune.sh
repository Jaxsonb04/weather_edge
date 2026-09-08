#!/usr/bin/env bash
# Archive-gated retention maintenance for paper_trading.db.
#
# Scheduled runs delete. Export, upload, the exact-coverage archive gate, and
# the FK audit run first, and only a passing archive gate unlocks deletion: no
# row leaves the live DB until every complete UTC day of every snapshot table
# is losslessly exported and verified (manifest-backed).
# Upload and feature-rollup failures are non-fatal: raw local archive files are
# the safety property; the 30-day ring buffer absorbs S3 outages, and features
# can always be rebuilt from the archive.
set -euo pipefail

TRADING_DIR="${SFO_TRADING_ROOT:-/opt/weatheredge/trading}"
PY="${SFO_TRADING_PYTHON:-$TRADING_DIR/.venv/bin/python}"
DB="${SFO_KALSHI_DB:-$TRADING_DIR/data/paper_trading.db}"
ARCHIVE_DIR="${SFO_ARCHIVE_DIR:-$TRADING_DIR/data/archive}"
# Retention modes (SFO_PRUNE_MODE):
#   bounded-delete  (default) nightly batched delete with the paper writers
#                   still running. `PaperStore.prune_decision_snapshots` commits
#                   every batch and shrinks the batch whenever it holds SQLite's
#                   write lock longer than --max-batch-seconds (2 s), while the
#                   scan/monitor writers wait on a 30 s busy_timeout.
#                   --max-batch-seconds is a shrink target measured AFTER each
#                   batch, not a ceiling: a batch always runs to completion at
#                   the current row limit, so the first batch of a run (full
#                   5,000 rows, cold page cache) can overrun it once before the
#                   limit halves.
#   quiesced-delete the same bounded delete plus an explicit operator assertion
#                   that every paper-journal writer has been stopped. Use it for
#                   a supervised catch-up run, not on the timer.
#   archive-only    escape hatch: archive, upload, gate, and FK audit only; the
#                   live DB is never written. Growth then continues at roughly
#                   0.7 GB/day and the deploy backup gate eventually fails.
PRUNE_MODE="${SFO_PRUNE_MODE:-bounded-delete}"
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
# `archive_gate_passed` is only set by the line immediately after the gate
# command, so it records that this exact run's gate returned success.
archive_gate_passed=0
"$PY" -m sfo_kalshi_quant.cli --no-color --db-path "$DB" \
  paper-archive --archive-dir "$ARCHIVE_DIR" --check-gate
archive_gate_passed=1

# 5. Explicit integrity audit (kept out of normal PaperStore initialization).
"$PY" -m sfo_kalshi_quant.cli --no-color --db-path "$DB" \
  paper-check-foreign-keys --limit "${SFO_FK_AUDIT_LIMIT:-100}"

# 6. Live-DB deletion. The archive, upload, exact-coverage gate, and FK audit
# above always run first and the gate is a hard interlock: deletion is refused
# unless this run's own `--check-gate` returned success.
case "$PRUNE_MODE" in
  bounded-delete|quiesced-delete)
    prune_requested=1
    ;;
  archive-only)
    prune_requested=0
    ;;
  *)
    echo "DEGRADED: unrecognized SFO_PRUNE_MODE=$PRUNE_MODE; failing closed to archive-only" >&2
    PRUNE_MODE="archive-only"
    prune_requested=0
    ;;
esac

# Unreachable while `set -euo pipefail` is in force, because a failed gate
# already aborts this script at step 4. It is kept as an explicit interlock so
# no future edit -- a `|| true` on the gate, a reordering, a new mode -- can
# quietly put a delete ahead of the archive that makes it recoverable.
if (( prune_requested == 1 )) && (( archive_gate_passed != 1 )); then
  echo "BLOCKED: archive/verify gate did not pass; refusing live-DB deletion" >&2
  exit 1
fi

# Set by the delete below and read after the ring-buffer cleanup, so a failed
# delete cannot take the cleanup down with it (see step 6's comment).
prune_status=0

if (( prune_requested == 1 )); then
  if [[ "$PRUNE_MODE" == "quiesced-delete" ]]; then
    echo "NOTICE: quiesced live-DB deletion explicitly enabled; paper writers must be stopped" >&2
  else
    echo "NOTICE: bounded live-DB deletion enabled; archive gate passed, each batch commits and releases the write lock, and a batch that overruns ${SFO_PRUNE_MAX_BATCH_SECONDS:-2}s halves the next batch's row limit" >&2
  fi

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

  # The delete and the ring-buffer cleanup in step 7 are peers, not a chain.
  # `paper-prune` fails on ordinary lock contention (SQLITE_BUSY, which cli.main
  # maps to exit 75), and under `set -e` that failure would also skip step 7 --
  # the only thing that bounds data/archive -- so a single contended night would
  # stop freeing disk in BOTH places at once, which is the exact failure this
  # default exists to prevent. Record the status, let the cleanup run, and fail
  # the unit at the very end.
  "$PY" -m sfo_kalshi_quant.cli --no-color --db-path "$DB" \
    paper-prune --full-days "${SFO_PRUNE_FULL_DAYS:-1}" --dedup-days "${SFO_PRUNE_DEDUP_DAYS:-45}" \
    --batch-limit "${SFO_PRUNE_BATCH_LIMIT:-5000}" \
    --max-batch-seconds "${SFO_PRUNE_MAX_BATCH_SECONDS:-2}" \
    --batch-pause-seconds "${SFO_PRUNE_BATCH_PAUSE_SECONDS:-0.15}" \
    || prune_status=$?

  if (( prune_status == 0 )); then
    echo "NOTICE: $PRUNE_MODE retention complete; the pruned-decision-snapshots line above is this run's actual delete count" >&2
  else
    echo "WARN: $PRUNE_MODE live-DB deletion failed (exit $prune_status); ring-buffer cleanup still runs and the unit fails after it" >&2
  fi
else
  echo "DEGRADED: archive/upload/gate/FK complete; scheduled live-DB deletion skipped by SFO_PRUNE_MODE=$PRUNE_MODE" >&2
  echo "DEGRADED: journal growth continues; disk watchdog remains the safety alarm" >&2
fi

# 7. Ring buffer: drop local copies >keep-days old ONLY if verifiably uploaded.
"$PY" -m sfo_kalshi_quant.cli --no-color --db-path "$DB" \
  paper-archive --archive-dir "$ARCHIVE_DIR" --cleanup --keep-days "${SFO_ARCHIVE_KEEP_DAYS:-30}" \
  || echo "WARN: ring-buffer cleanup failed" >&2

# 8. Report the delete's failure last, after the cleanup above has had its turn.
# The unit still fails -- exit 75 is what systemd's OnFailure hook and the
# operator need to see -- it just no longer takes the disk-freeing step with it.
if (( prune_status != 0 )); then
  echo "ERROR: live-DB deletion failed (exit $prune_status); archive, gate, and ring-buffer cleanup completed" >&2
  exit "$prune_status"
fi
