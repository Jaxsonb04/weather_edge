#!/usr/bin/env bash
# Bounded retry for a CLI invocation that exited 75 (EX_TEMPFAIL) because
# another writer held the SQLite write lock past busy_timeout.
#
# Why this exists (audit F-02, 2026-08-07): the paper scan and monitor are
# Type=oneshot units, and systemd forbids Restart= on oneshot, so an exit 75
# lands as a FAILED unit plus an sfo-alert page and the tick is LOST rather
# than deferred. run_dataset_backfill.sh already solved this shape for the
# nightly backfill; this is the same loop, retuned.
#
# Retuned how: the backfill is BUDGET-bound (one 1800 s service deadline shared
# across sources), while scan and monitor are LATENCY-bound -- their own timers
# refire in 300 s and 120 s. A retry is only worth doing if it lands well inside
# that cadence; past it, skipping is strictly better than two overlapping ticks.
# Hence low attempt and delay ceilings, validated fail-fast like the backfill.
#
# A retry is behaviourally safe for both callers: SQLite rolled the failed
# transaction back, the scan runs inside run_paper_scan_profiles.sh's flock so
# no second scan can interleave, and the monitor is already re-entrant every
# 120 s -- a retry is just the next tick arriving early.
set -euo pipefail

ATTEMPTS="${SFO_PAPER_LOCK_RETRY_ATTEMPTS:-2}"
DELAY="${SFO_PAPER_LOCK_RETRY_DELAY_SECONDS:-15}"
MAX_ATTEMPTS=4
MAX_DELAY=45

if [[ ! "$ATTEMPTS" =~ ^[1-9]$ ]] || (( 10#$ATTEMPTS > MAX_ATTEMPTS )); then
  echo "SFO_PAPER_LOCK_RETRY_ATTEMPTS must be a canonical integer from 1 to $MAX_ATTEMPTS" >&2
  exit 2
fi
if [[ ! "$DELAY" =~ ^([0-9]|[1-9][0-9])$ ]] || (( 10#$DELAY > MAX_DELAY )); then
  echo "SFO_PAPER_LOCK_RETRY_DELAY_SECONDS must be a canonical integer from 0 to $MAX_DELAY" >&2
  exit 2
fi
ATTEMPTS=$((10#$ATTEMPTS))
DELAY=$((10#$DELAY))

if (( $# == 0 )); then
  echo "usage: retry_on_sqlite_lock.sh <command> [args...]" >&2
  exit 2
fi

status=0
attempt=1
while (( attempt <= ATTEMPTS )); do
  status=0
  "$@" || status=$?
  if (( status != 75 )); then
    exit "$status"
  fi
  if (( attempt >= ATTEMPTS )); then
    echo "warning: SQLite lock persisted across $ATTEMPTS attempt(s); this tick is skipped" >&2
    exit "$status"
  fi
  echo "warning: transient SQLite lock; retrying ($attempt/$ATTEMPTS) in ${DELAY}s" >&2
  sleep "$DELAY"
  ((attempt += 1))
done
exit "$status"
