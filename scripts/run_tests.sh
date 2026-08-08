#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [[ -x .venv-dev/bin/pytest ]]; then
  PYTEST=(.venv-dev/bin/pytest)
else
  # Audit F-07: the fallback interpreter must actually be able to run this
  # project. macOS resolves `python3` to the Xcode 3.9 build, which cannot parse
  # the codebase; the resulting "No module named pytest" hid the real cause.
  if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    echo "python3 on PATH is $(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || echo 'unknown')," >&2
    echo "but this project requires 3.11+. Create the project virtualenv first:" >&2
    echo "  python3.13 -m venv .venv-dev && .venv-dev/bin/pip install -e . pytest" >&2
    echo "or run the suite with an explicit interpreter:" >&2
    echo "  PYTHONPATH=trading:forecaster /path/to/python -m pytest trading/tests forecaster/tests" >&2
    exit 1
  fi
  PYTEST=(python3 -m pytest)
fi
PYTHONPATH=trading:forecaster "${PYTEST[@]}" trading/tests forecaster/tests -q
