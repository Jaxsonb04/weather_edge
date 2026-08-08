#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Audit F-07: prefer the project's own interpreter, mirroring
# scripts/run_tests.sh. A bare `python3` is the Xcode 3.9 build on macOS, which
# cannot even parse this codebase, so `compileall` reported failures that had
# nothing to do with the source.
if [[ -x .venv-dev/bin/python ]]; then
  PYTHON=(.venv-dev/bin/python)
else
  PYTHON=(python3)
fi

"${PYTHON[@]}" scripts/weatheredge_health_check.py "$@"
bash scripts/run_semgrep.sh
bash scripts/run_tests.sh
"${PYTHON[@]}" -m compileall forecaster trading/sfo_kalshi_quant trading/tests scripts
