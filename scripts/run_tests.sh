#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [[ -x .venv-dev/bin/pytest ]]; then
  PYTEST=(.venv-dev/bin/pytest)
else
  PYTEST=(python3 -m pytest)
fi
PYTHONPATH=trading:forecaster "${PYTEST[@]}" trading/tests forecaster/tests -q
