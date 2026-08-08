#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PY_MINOR="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
USER_PY_BIN="$HOME/Library/Python/$PY_MINOR/bin"
if [[ -x "$USER_PY_BIN/semgrep" ]]; then
  export PATH="$USER_PY_BIN:$PATH"
fi

if ! command -v semgrep >/dev/null 2>&1; then
  # Audit F-08: CI installs a pinned Semgrep and must keep failing closed. A
  # local checkout usually has none, and failing hard there aborted
  # verify_project.sh before the tests and compile check it gates ever ran --
  # which made the local gate strictly worse than no gate. Warn loudly and
  # skip locally; stay a hard failure wherever Semgrep is genuinely required.
  require_semgrep="${WEATHEREDGE_REQUIRE_SEMGREP:-${CI:-}}"
  case "$require_semgrep" in
    1 | true | TRUE | True)
      echo "Semgrep CLI is required here but was not found." >&2
      echo "Install with: python3 -m pip install --user semgrep" >&2
      exit 1
      ;;
  esac
  echo "WARN: Semgrep CLI not found; skipping the static-analysis gate." >&2
  echo "WARN: this gate still runs in CI. Install it locally with:" >&2
  echo "WARN:   python3 -m pip install --user semgrep" >&2
  echo "WARN: set WEATHEREDGE_REQUIRE_SEMGREP=1 to make its absence fatal." >&2
  exit 0
fi

if python3 -c 'import certifi' >/dev/null 2>&1; then
  export SSL_CERT_FILE="${SSL_CERT_FILE:-$(python3 -c 'import certifi; print(certifi.where())')}"
fi

export SEMGREP_SEND_METRICS=off
TMP_ROOT="${TMPDIR:-/tmp}"
export SEMGREP_LOG_FILE="${SEMGREP_LOG_FILE:-$TMP_ROOT/weatheredge-semgrep.log}"
export SEMGREP_VERSION_CACHE_PATH="${SEMGREP_VERSION_CACHE_PATH:-$TMP_ROOT/weatheredge-semgrep-version}"

semgrep scan \
  --disable-version-check \
  --metrics=off \
  --config "$ROOT_DIR/.semgrep/weatheredge.yml" \
  --error \
  "$ROOT_DIR"
