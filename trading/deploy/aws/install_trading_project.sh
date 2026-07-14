#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
if [[ ! -f "$SCRIPT_DIR/verify_trading_install.py" ]]; then
  echo "missing trading install verifier: $SCRIPT_DIR/verify_trading_install.py" >&2
  exit 1
fi

VENV_DIR="$(cd "$(dirname "$PYTHON_BIN")/.." && pwd -P)"
PURELIB="$("$PYTHON_BIN" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
case "$PURELIB/" in
  "$VENV_DIR/"*) ;;
  *)
    echo "trading Python site-packages escaped its virtualenv: $PURELIB" >&2
    exit 1
    ;;
esac

# Interrupted or formerly root-run pip upgrades can leave pip's temporary
# renamed metadata behind. importlib.metadata still counts that directory as a
# second WeatherEdge owner even though pip warns that it is invalid. Remove only
# pip's exact temporary name inside this verified virtualenv before upgrading.
find "$PURELIB" -maxdepth 1 -type d -name '~eatheredge-*.dist-info' \
  -exec rm -rf -- {} +

# TP-12 replaced the old sfo-kalshi-quant distribution with the root
# weatheredge project. Explicitly uninstall the old owner before installing so
# an upgraded venv cannot retain duplicate metadata or a stale console script.
env -u PYTHONPATH "$PYTHON_BIN" -m pip uninstall -y sfo-kalshi-quant
# Legacy editable installs leave source-tree metadata behind even after pip
# removes their site-packages link. The new root editable install adds trading/
# to sys.path, so that one exact stale directory would otherwise resurrect the
# retired distribution in importlib.metadata.
rm -rf -- "$TRADING_DIR/sfo_kalshi_quant.egg-info"
env -u PYTHONPATH "$PYTHON_BIN" -m pip install \
  --no-build-isolation --no-deps -e "$BASE_DIR"
# Setuptools creates source-tree egg metadata while building an editable wheel.
# The installed dist-info is authoritative; remove the exact transient source
# copy so importlib.metadata observes one owner object, not two identical ones.
rm -rf -- "$TRADING_DIR/weatheredge.egg-info"
env -u PYTHONPATH "$PYTHON_BIN" "$SCRIPT_DIR/verify_trading_install.py"
