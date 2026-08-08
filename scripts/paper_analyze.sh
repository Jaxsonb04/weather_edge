#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
profiles_csv="${PAPER_RISK_PROFILES:-${PAPER_RISK_PROFILE:-live}}"
IFS=',' read -r -a profiles <<< "$profiles_csv"
for raw_profile in "${profiles[@]}"; do
  profile="${raw_profile//[[:space:]]/}"
  if [[ -z "$profile" ]]; then
    continue
  fi
  echo "running paper analysis profile=$profile"
  PYTHONPATH=trading "$PYTHON_BIN" -m sfo_kalshi_quant.cli --no-color --risk-profile "$profile" analyze --target-date both --side both "$@"
done
