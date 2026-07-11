#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="${1:?usage: install_trading_project.sh BASE_DIR PYTHON_BIN}"
PYTHON_BIN="${2:?usage: install_trading_project.sh BASE_DIR PYTHON_BIN}"
TRADING_DIR="${TRADING_DIR:-$BASE_DIR/trading}"

if [[ ! -f "$BASE_DIR/pyproject.toml" || ! -f "$BASE_DIR/README.md" ]]; then
  echo "missing root Python project at $BASE_DIR" >&2
  exit 1
fi
if [[ -f "$TRADING_DIR/pyproject.toml" ]]; then
  echo "legacy nested Python manifest remains at $TRADING_DIR/pyproject.toml; run sync_to_box.sh first" >&2
  exit 1
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "trading Python is not executable: $PYTHON_BIN" >&2
  exit 1
fi

# TP-12 replaced the old sfo-kalshi-quant distribution with the root
# weatheredge project. Explicitly uninstall the old owner before installing so
# an upgraded venv cannot retain duplicate metadata or a stale console script.
"$PYTHON_BIN" -m pip uninstall -y sfo-kalshi-quant
# Legacy editable installs leave source-tree metadata behind even after pip
# removes their site-packages link. The new root editable install adds trading/
# to sys.path, so that one exact stale directory would otherwise resurrect the
# retired distribution in importlib.metadata.
rm -rf -- "$TRADING_DIR/sfo_kalshi_quant.egg-info"
"$PYTHON_BIN" -m pip install -e "$BASE_DIR"

"$PYTHON_BIN" - <<'PY'
from __future__ import annotations

import re
from importlib.metadata import distributions


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


projects = [
    dist
    for dist in distributions()
    if canonical(dist.metadata.get("Name", ""))
    in {"weatheredge", "sfo-kalshi-quant"}
]
names = sorted({canonical(dist.metadata.get("Name", "")) for dist in projects})
if names != ["weatheredge"]:
    raise SystemExit(f"invalid WeatherEdge distribution ownership: {names}")

scripts = {
    entry.value
    for entry in projects[0].entry_points
    if entry.group == "console_scripts" and entry.name == "sfo-kalshi"
}
for project in projects[1:]:
    scripts.update(
        entry.value
        for entry in project.entry_points
        if entry.group == "console_scripts" and entry.name == "sfo-kalshi"
    )
if scripts != {"sfo_kalshi_quant.cli:main"}:
    raise SystemExit(f"invalid sfo-kalshi console ownership: {scripts}")
PY

echo "verified weatheredge as the sole project owner of sfo-kalshi"
