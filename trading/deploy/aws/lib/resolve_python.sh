# WeatherEdge Python interpreter resolution (audit F-07).
#
# POSIX sh. Source this file; do not execute it.
#
# The repository requires Python >= 3.11 (pyproject `requires-python`). A bare
# `python3` resolves to whatever is first on PATH; on the operator's macOS build
# machine that is /Applications/Xcode.app/.../Versions/3.9, which dies with
#   ImportError: cannot import name 'UTC' from 'datetime'
# raised from trading/sfo_kalshi_quant/maker_fills.py line 21. sync_to_box.sh
# hit this AFTER it had already quiesced production timers, so a deploy could
# strand the box. Every script that runs repository Python resolves here.
#
# Two entry points, both echoing the resolved interpreter on stdout, writing
# diagnostics to stderr, and returning non-zero instead of exiting:
#
#   weatheredge_require_python <interpreter>
#     Validate exactly ONE interpreter. On-box runtime scripts use this: the
#     pinned .venv is the only correct interpreter there, and falling back to
#     the system python3 would run trading code without its locked deps.
#
#   weatheredge_resolve_python [preferred...]
#     Search and validate. Operator and developer scripts use this.

WEATHEREDGE_MIN_PYTHON_MAJOR=3
WEATHEREDGE_MIN_PYTHON_MINOR=11

# Fallback order: the environment's own `python3` first, so a correctly
# provisioned machine keeps using what it intends; then the versions production
# and CI actually run; then the oldest supported. Interpreters NEWER than CI
# covers are deliberately not searched -- the repo is not validated against them.
WEATHEREDGE_PYTHON_FALLBACKS="python3 python3.13 python3.12 python3.11"

weatheredge_python_version() {
  "$1" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null \
    || printf 'unknown'
}

weatheredge_python_is_supported() {
  command -v "$1" >/dev/null 2>&1 || return 1
  "$1" -c "import sys; raise SystemExit(0 if sys.version_info >= ($WEATHEREDGE_MIN_PYTHON_MAJOR, $WEATHEREDGE_MIN_PYTHON_MINOR) else 1)" \
    >/dev/null 2>&1
}

weatheredge_require_python() {
  _we_candidate="${1:-}"
  if [ -z "$_we_candidate" ] || ! command -v "$_we_candidate" >/dev/null 2>&1; then
    echo "missing trading Python runtime: ${_we_candidate:-<unset>}" >&2
    return 1
  fi
  if ! weatheredge_python_is_supported "$_we_candidate"; then
    echo "unsupported Python runtime: $_we_candidate is $(weatheredge_python_version "$_we_candidate"); WeatherEdge requires >= ${WEATHEREDGE_MIN_PYTHON_MAJOR}.${WEATHEREDGE_MIN_PYTHON_MINOR}" >&2
    return 1
  fi
  command -v "$_we_candidate"
}

weatheredge_resolve_python() {
  if [ -n "${SFO_TRADING_PYTHON:-}" ]; then
    # An explicit override is never silently ignored or fallen back from.
    if ! weatheredge_require_python "$SFO_TRADING_PYTHON"; then
      echo "(interpreter came from SFO_TRADING_PYTHON)" >&2
      return 1
    fi
    return 0
  fi
  # shellcheck disable=SC2086  # intentional word splitting of the fallback list
  for _we_candidate in "$@" $WEATHEREDGE_PYTHON_FALLBACKS; do
    [ -n "$_we_candidate" ] || continue
    if weatheredge_python_is_supported "$_we_candidate"; then
      command -v "$_we_candidate"
      return 0
    fi
  done
  echo "no Python >= ${WEATHEREDGE_MIN_PYTHON_MAJOR}.${WEATHEREDGE_MIN_PYTHON_MINOR} found." >&2
  echo "tried: $* $WEATHEREDGE_PYTHON_FALLBACKS" >&2
  echo "set SFO_TRADING_PYTHON to a supported interpreter (repo dev venv: .venv-dev/bin/python)." >&2
  return 1
}
