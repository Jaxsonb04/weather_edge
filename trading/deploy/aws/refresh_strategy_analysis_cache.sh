#!/usr/bin/env bash
# Refresh the expensive Strategy Lab analysis from a verified deploy snapshot.
# Recurring publication never calls this helper.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${WEATHEREDGE_ENV_FILE:-${SFO_WEATHEREDGE_ENV_FILE:-/etc/weatheredge.env}}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  if [[ -r "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
  else
    # Production installs the trusted environment as root-owned mode 600.
    # Capture it through the deploy user's existing passwordless sudo rather
    # than weakening permissions on secrets. Assignment preserves `set -e`:
    # an unreadable file aborts before any service or artifact mutation.
    env_contents="$(sudo -n cat -- "$ENV_FILE")"
    # shellcheck disable=SC1091
    source /dev/stdin <<<"$env_contents"
    unset env_contents
  fi
  set +a
fi
TRADING_DIR="${SFO_TRADING_ROOT:-/opt/weatheredge/trading}"
FORECASTER_DIR="${SFO_FORECASTER_ROOT:-/opt/weatheredge/forecaster}"
PYTHON_BIN="${SFO_TRADING_PYTHON:-$TRADING_DIR/.venv/bin/python}"
TIMEOUT_SECONDS="${SFO_STRATEGY_FULL_ANALYSIS_TIMEOUT_SECONDS:-1800}"
ANALYSIS_LOCK="${SFO_STRATEGY_ANALYSIS_LOCK:-/opt/weatheredge/.locks/strategy-analysis.lock}"
LIVE_DB_PATH="${SFO_KALSHI_DB:-$TRADING_DIR/data/paper_trading.db}"
ANALYSIS_DB_PATH="${SFO_STRATEGY_ANALYSIS_DB_PATH:-}"

if [[ ! "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "SFO_STRATEGY_FULL_ANALYSIS_TIMEOUT_SECONDS must be a positive integer" >&2
  exit 1
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "missing trading Python runtime: $PYTHON_BIN" >&2
  exit 1
fi
if ! command -v systemd-run >/dev/null 2>&1; then
  echo "systemd-run is required for memory-contained full analysis" >&2
  exit 1
fi
if [[ -z "$ANALYSIS_DB_PATH" || ! -f "$ANALYSIS_DB_PATH" ]]; then
  echo "SFO_STRATEGY_ANALYSIS_DB_PATH must name a verified database snapshot" >&2
  exit 1
fi
if [[ -e "$LIVE_DB_PATH" ]] && "$PYTHON_BIN" -c \
  'import os, sys; raise SystemExit(0 if os.path.samefile(sys.argv[1], sys.argv[2]) else 1)' \
  "$ANALYSIS_DB_PATH" "$LIVE_DB_PATH"; then
  echo "analysis snapshot must differ from the live paper database" >&2
  exit 1
fi
analysis_db_real="$("$PYTHON_BIN" -c 'import pathlib, sys; print(pathlib.Path(sys.argv[1]).resolve())' "$ANALYSIS_DB_PATH")"
ANALYSIS_DB_PATH="$analysis_db_real"

"$PYTHON_BIN" - "$FORECASTER_DIR/build_info.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"deployed build_info.json is unavailable or invalid: {exc}")
if not isinstance(payload.get("source_sha"), str) or not payload["source_sha"].strip():
    raise SystemExit("deployed build_info.json is missing source_sha")
if not isinstance(payload.get("source_dirty"), bool):
    raise SystemExit("deployed build_info.json is missing boolean source_dirty")
PY

mkdir -p "$(dirname "$ANALYSIS_LOCK")" "$FORECASTER_DIR"
exec 8>"$ANALYSIS_LOCK"
if ! flock -n 8; then
  echo "another full Strategy Lab analysis is already running" >&2
  exit 1
fi

stage_dir="$(mktemp -d "${TMPDIR:-/tmp}/weatheredge-analysis.XXXXXX")"
stage_output="$stage_dir/strategy_research.json"
stage_cache="$stage_dir/strategy_analysis_cache.json"
stage_evidence="$stage_dir/strategy_research_evidence.private.json"
cache_output="$FORECASTER_DIR/strategy_analysis_cache.json"
evidence_output="$FORECASTER_DIR/strategy_research_evidence.private.json"
promote_tmp="${cache_output}.promote.$$"
evidence_promote_tmp="${evidence_output}.promote.$$"
analysis_unit="weatheredge-strategy-analysis-cache.service"
analysis_started=0
cleanup() {
  local status=$?
  trap - EXIT HUP INT TERM
  if (( analysis_started == 1 )); then
    sudo -n systemctl stop "$analysis_unit" >/dev/null 2>&1 || true
    for _ in {1..50}; do
      if ! sudo -n systemctl is-active --quiet "$analysis_unit"; then
        break
      fi
      sleep 0.2
    done
    sudo -n systemctl reset-failed "$analysis_unit" >/dev/null 2>&1 || true
  fi
  rm -rf -- "$stage_dir"
  rm -f -- "$promote_tmp" "$evidence_promote_tmp"
  exit "$status"
}
trap cleanup EXIT HUP INT TERM

analysis_user="$(id -un)"
analysis_group="$(id -gn)"
# If an earlier transport died after releasing the wrapper lock, this stable
# name identifies the orphan exactly. A live wrapper still owns the flock and
# returned above, so stopping an active unit here cannot interrupt a peer run.
if sudo -n systemctl is-active --quiet "$analysis_unit"; then
  sudo -n systemctl stop "$analysis_unit"
fi
sudo -n systemctl reset-failed "$analysis_unit" >/dev/null 2>&1 || true
systemd_properties=(
  "--property=User=$analysis_user"
  "--property=Group=$analysis_group"
  "--property=WorkingDirectory=$TRADING_DIR"
  "--property=EnvironmentFile=$ENV_FILE"
  "--property=MemoryHigh=1200M"
  "--property=MemoryMax=1600M"
  "--property=MemorySwapMax=512M"
  "--property=RuntimeMaxSec=${TIMEOUT_SECONDS}s"
  "--property=TimeoutStopSec=30s"
  "--property=Nice=10"
  "--property=IOSchedulingClass=best-effort"
  "--property=IOSchedulingPriority=7"
)
analysis_started=1
sudo -n systemd-run \
  --wait \
  --pipe \
  --collect \
  --quiet \
  "--unit=$analysis_unit" \
  "${systemd_properties[@]}" \
  /usr/bin/env \
  SFO_STRATEGY_FAST_PUBLICATION=0 \
  SFO_STRATEGY_BUILD_STAGING=1 \
  "SFO_KALSHI_DB=$ANALYSIS_DB_PATH" \
  "SFO_STRATEGY_RESEARCH_PATH=$stage_output" \
  /bin/bash "$SCRIPT_DIR/build_strategy_research.sh"

if [[ ! -f "$stage_cache" ]]; then
  echo "full Strategy Lab analysis did not produce its cache" >&2
  exit 1
fi
if [[ ! -f "$stage_evidence" ]]; then
  echo "full Strategy Lab analysis did not produce private replay evidence" >&2
  exit 1
fi

"$PYTHON_BIN" - "$stage_cache" "$stage_evidence" "$FORECASTER_DIR/build_info.json" <<'PY'
import json
import sys
from pathlib import Path

cache_path = Path(sys.argv[1])
evidence_path = Path(sys.argv[2])
build_info_path = Path(sys.argv[3])
cache = json.loads(cache_path.read_text(encoding="utf-8"))
evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
build_info = (
    json.loads(build_info_path.read_text(encoding="utf-8"))
    if build_info_path.exists()
    else {}
)
source_sha = build_info.get("source_sha")
source_dirty = build_info.get("source_dirty")
if not isinstance(source_sha, str) or not source_sha.strip():
    raise SystemExit("deployed build_info source_sha is unavailable")
if not isinstance(source_dirty, bool):
    raise SystemExit("deployed build_info source_dirty is invalid")
expected = (
    f"{source_sha}:dirty"
    if source_dirty is True
    else source_sha
)
if cache.get("schema_version") != 2:
    raise SystemExit("Strategy Lab analysis cache schema is not current")
if cache.get("source_sha") != expected:
    raise SystemExit("Strategy Lab analysis cache source does not match build_info")
for field in (
    "config_fingerprint",
    "analysis_generated_at",
    "backtest_summary",
    "config_rescore",
    "chronological_replay",
    "research_shadow",
    "forecast_scorecards",
    "daily_summary_analysis",
):
    if not cache.get(field):
        raise SystemExit(f"Strategy Lab analysis cache is missing {field}")
if cache["daily_summary_analysis"].get("available") is not True:
    raise SystemExit("Strategy Lab daily summary analysis is unavailable")
if evidence.get("source_sha") != expected:
    raise SystemExit("Strategy Lab private evidence source does not match build_info")
if evidence.get("config_fingerprint") != cache.get("config_fingerprint"):
    raise SystemExit("Strategy Lab private evidence config does not match cache")
if evidence.get("analysis_generated_at") != cache.get("analysis_generated_at"):
    raise SystemExit("Strategy Lab private evidence timestamp does not match cache")
if not isinstance(evidence.get("chronological_replay"), dict):
    raise SystemExit("Strategy Lab private replay evidence is missing")
PY

cp -- "$stage_evidence" "$evidence_promote_tmp"
mv -f -- "$evidence_promote_tmp" "$evidence_output"
cp -- "$stage_cache" "$promote_tmp"
mv -f -- "$promote_tmp" "$cache_output"
echo "full Strategy Lab analysis cache and private evidence refreshed from the current deployed source"
