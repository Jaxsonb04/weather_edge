#!/usr/bin/env bash
# Run every nightly forecaster step, then fail if any step failed.
set -euo pipefail

FORECASTER_DIR="${1:?forecaster directory is required}"
PYTHON_BIN="$FORECASTER_DIR/.venv/bin/python"
rc=0
"$PYTHON_BIN" "$FORECASTER_DIR/city_truth.py" --db "$FORECASTER_DIR/weather.db" --backfill-iem --start-year 2026 --cities all || rc=1
"$PYTHON_BIN" "$FORECASTER_DIR/nwp_archive.py" --db "$FORECASTER_DIR/weather.db" --daily --cities all || rc=1
"$PYTHON_BIN" "$FORECASTER_DIR/emos_forecast.py" --db "$FORECASTER_DIR/weather.db" --backfill --lead 1 --cities all || rc=1
"$PYTHON_BIN" "$FORECASTER_DIR/emos_forecast.py" --db "$FORECASTER_DIR/weather.db" --backfill --lead 2 --cities all || rc=1
exit "$rc"
