#!/usr/bin/env bash
set -euo pipefail

# Wait for GitHub Pages to expose the exact manifest just published locally.
# This closes the deploy-time propagation race without weakening freshness
# thresholds or generating a false watchdog failure/alert.
BASE_DIR="${SFO_BASE_DIR:-${BASE_DIR:-/opt/weatheredge}}"
FORECASTER_DIR="${SFO_FORECASTER_ROOT:-$BASE_DIR/forecaster}"
TRADING_DIR="${SFO_TRADING_ROOT:-$BASE_DIR/trading}"
PYTHON_BIN="${SFO_TRADING_PYTHON:-$TRADING_DIR/.venv/bin/python}"
MANIFEST="${SFO_PUBLICATION_MANIFEST_PATH:-$FORECASTER_DIR/publication_manifest.json}"
ENV_FILE="${SFO_WEATHEREDGE_ENV_FILE:-/etc/weatheredge.env}"
TIMEOUT_SECONDS="${SFO_PUBLICATION_PROPAGATION_TIMEOUT_SECONDS:-300}"
POLL_SECONDS="${SFO_PUBLICATION_PROPAGATION_POLL_SECONDS:-5}"

env_value() {
  local name="$1"
  local current="${!name:-}"
  if [[ -n "$current" ]]; then
    printf '%s' "$current"
    return
  fi
  if [[ -f "$ENV_FILE" ]]; then
    sudo awk -F= -v key="$name" '
      $1 == key {
        sub(/^[^=]*=/, "")
        sub(/\r$/, "")
        print
        exit
      }
    ' "$ENV_FILE"
  fi
}

PUBLISH_PAGES="$(env_value SFO_PUBLISH_PAGES)"
PUBLIC_MANIFEST_URL="$(env_value SFO_PUBLICATION_MANIFEST_URL)"
PUBLIC_MANIFEST_URL="${PUBLIC_MANIFEST_URL:-$(env_value SFO_PUBLIC_MANIFEST_URL)}"

if [[ "${PUBLISH_PAGES:-0}" != "1" ]]; then
  echo "GitHub Pages publishing disabled; public propagation wait skipped"
  exit 0
fi
if [[ -z "$PUBLIC_MANIFEST_URL" ]]; then
  echo "SFO_PUBLICATION_MANIFEST_URL is required when SFO_PUBLISH_PAGES=1" >&2
  exit 1
fi
if [[ ! -f "$MANIFEST" || ! -x "$PYTHON_BIN" ]]; then
  echo "local publication manifest or Python runtime unavailable" >&2
  exit 1
fi
if [[ ! "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ || ! "$POLL_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "publication propagation timeout and poll interval must be positive integers" >&2
  exit 1
fi

public_tmp="$(mktemp "${TMPDIR:-/tmp}/weatheredge-public-wait.XXXXXX")"
trap 'rm -f "$public_tmp"' EXIT HUP INT TERM
deadline=$((SECONDS + TIMEOUT_SECONDS))

while (( SECONDS < deadline )); do
  separator='?'
  [[ "$PUBLIC_MANIFEST_URL" == *'?'* ]] && separator='&'
  if curl -fsS -m 15 "${PUBLIC_MANIFEST_URL}${separator}snapshot_wait=$(date +%s)" >"$public_tmp" \
    && "$PYTHON_BIN" - "$MANIFEST" "$public_tmp" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    local = json.load(handle)
with open(sys.argv[2], encoding="utf-8") as handle:
    public = json.load(handle)

matches = (
    public.get("snapshot_id") == local.get("snapshot_id")
    and public.get("provenance", {}).get("source_sha")
    == local.get("provenance", {}).get("source_sha")
)
raise SystemExit(0 if matches else 1)
PY
  then
    echo "public publication snapshot matches local manifest"
    exit 0
  fi
  sleep "$POLL_SECONDS"
done

echo "timed out waiting for the public publication snapshot after ${TIMEOUT_SECONDS}s" >&2
exit 1
