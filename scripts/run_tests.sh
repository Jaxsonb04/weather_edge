#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
PYTHONPATH=trading python3 trading/tests/run_tests.py
PYTHONPATH=trading:forecaster python3 -m pytest forecaster/tests -q
