#!/usr/bin/env bash
# Timer-less variant of install_systemd.sh used for migrations/cutover: it
# renders and installs every unit file but does NOT enable any timer, so a
# freshly provisioned box stays inert until the operator flips the timers on.
set -euo pipefail

APP_USER="${APP_USER:-ubuntu}"
BASE_DIR="${BASE_DIR:-/opt/weatheredge}"
TRADING_DIR="${TRADING_DIR:-$BASE_DIR/trading}"
FORECASTER_DIR="${FORECASTER_DIR:-$BASE_DIR/forecaster}"
ENV_FILE="${ENV_FILE:-/etc/weatheredge.env}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -d "$TRADING_DIR/sfo_kalshi_quant" ]]; then
  echo "missing trading repo at $TRADING_DIR" >&2
  exit 1
fi

if [[ ! -f "$FORECASTER_DIR/google_weather_cache.py" ]]; then
  echo "missing forecaster repo at $FORECASTER_DIR" >&2
  exit 1
fi

# Established hosts may already have enabled timers. Stop and disable every
# known timer before changing dependencies or units; real failures abort.
bash "$SCRIPT_DIR/disable_systemd_timers.sh"

sudo timedatectl set-timezone America/Los_Angeles
sudo apt-get update
sudo apt-get install -y curl git python3 python3-venv python3-pip sqlite3 rsync

mkdir -p "$TRADING_DIR/data" "$TRADING_DIR/logs" "$FORECASTER_DIR/logs"

if [[ ! -d "$TRADING_DIR/.venv" ]]; then
  python3 -m venv "$TRADING_DIR/.venv"
fi
"$TRADING_DIR/.venv/bin/python" -m pip install --upgrade pip
"$TRADING_DIR/.venv/bin/python" -m pip install -e "$TRADING_DIR"

if [[ ! -d "$FORECASTER_DIR/.venv" ]]; then
  python3 -m venv "$FORECASTER_DIR/.venv"
fi
"$FORECASTER_DIR/.venv/bin/python" -m pip install --upgrade pip
"$FORECASTER_DIR/.venv/bin/python" -m pip install certifi numpy pandas

if [[ ! -f "$ENV_FILE" ]]; then
  sudo install -m 600 "$SCRIPT_DIR/sfo-weather.env.example" "$ENV_FILE"
  echo "created $ENV_FILE"
fi

render_unit() {
  local src="$1"
  local dst="$2"
  sed \
    -e "s#__APP_USER__#$APP_USER#g" \
    -e "s#__TRADING_DIR__#$TRADING_DIR#g" \
    -e "s#__FORECASTER_DIR__#$FORECASTER_DIR#g" \
    -e "s#__ENV_FILE__#$ENV_FILE#g" \
    "$src" | sudo tee "$dst" >/dev/null
}

render_unit "$SCRIPT_DIR/systemd/sfo-forecaster-refresh.service.in" /etc/systemd/system/sfo-forecaster-refresh.service
render_unit "$SCRIPT_DIR/systemd/sfo-operational-publish.service.in" /etc/systemd/system/sfo-operational-publish.service
render_unit "$SCRIPT_DIR/systemd/sfo-strategy-lab-refresh.service.in" /etc/systemd/system/sfo-strategy-lab-refresh.service
render_unit "$SCRIPT_DIR/systemd/sfo-dataset-backfill.service.in" /etc/systemd/system/sfo-dataset-backfill.service
render_unit "$SCRIPT_DIR/systemd/sfo-kalshi-paper-scan.service.in" /etc/systemd/system/sfo-kalshi-paper-scan.service
render_unit "$SCRIPT_DIR/systemd/sfo-kalshi-paper-monitor.service.in" /etc/systemd/system/sfo-kalshi-paper-monitor.service
render_unit "$SCRIPT_DIR/systemd/sfo-kalshi-paper-settle.service.in" /etc/systemd/system/sfo-kalshi-paper-settle.service
render_unit "$SCRIPT_DIR/systemd/sfo-kalshi-paper-prune.service.in" /etc/systemd/system/sfo-kalshi-paper-prune.service
render_unit "$SCRIPT_DIR/systemd/sfo-forecast-freshness.service.in" /etc/systemd/system/sfo-forecast-freshness.service
render_unit "$SCRIPT_DIR/systemd/sfo-alert@.service.in" /etc/systemd/system/sfo-alert@.service

chmod +x "$SCRIPT_DIR/check_forecast_db_freshness.sh" "$SCRIPT_DIR/send_systemd_failure_alert.sh" 2>/dev/null || true

sudo install -m 644 "$SCRIPT_DIR/systemd/sfo-forecaster-refresh.timer" /etc/systemd/system/sfo-forecaster-refresh.timer
sudo install -m 644 "$SCRIPT_DIR/systemd/sfo-operational-publish.timer" /etc/systemd/system/sfo-operational-publish.timer
sudo install -m 644 "$SCRIPT_DIR/systemd/sfo-strategy-lab-refresh.timer" /etc/systemd/system/sfo-strategy-lab-refresh.timer
sudo install -m 644 "$SCRIPT_DIR/systemd/sfo-dataset-backfill.timer" /etc/systemd/system/sfo-dataset-backfill.timer
sudo install -m 644 "$SCRIPT_DIR/systemd/sfo-kalshi-paper-scan.timer" /etc/systemd/system/sfo-kalshi-paper-scan.timer
sudo install -m 644 "$SCRIPT_DIR/systemd/sfo-kalshi-paper-monitor.timer" /etc/systemd/system/sfo-kalshi-paper-monitor.timer
sudo install -m 644 "$SCRIPT_DIR/systemd/sfo-kalshi-paper-settle.timer" /etc/systemd/system/sfo-kalshi-paper-settle.timer
sudo install -m 644 "$SCRIPT_DIR/systemd/sfo-kalshi-paper-prune.timer" /etc/systemd/system/sfo-kalshi-paper-prune.timer
sudo install -m 644 "$SCRIPT_DIR/systemd/sfo-forecast-freshness.timer" /etc/systemd/system/sfo-forecast-freshness.timer

sudo systemctl daemon-reload
echo "units rendered and installed; all WeatherEdge timers remain disabled"
