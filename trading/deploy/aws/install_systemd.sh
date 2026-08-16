#!/usr/bin/env bash
set -euo pipefail

APP_USER="${APP_USER:-ubuntu}"
BASE_DIR="${BASE_DIR:-/opt/weatheredge}"
TRADING_DIR="${TRADING_DIR:-$BASE_DIR/trading}"
FORECASTER_DIR="${FORECASTER_DIR:-$BASE_DIR/forecaster}"
ENV_FILE="${ENV_FILE:-/etc/weatheredge.env}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "$BASE_DIR/pyproject.toml" || ! -f "$BASE_DIR/README.md" ]]; then
  echo "missing root Python project at $BASE_DIR; run sync_to_box.sh first" >&2
  exit 1
fi
if [[ -f "$TRADING_DIR/pyproject.toml" ]]; then
  echo "legacy nested Python manifest remains at $TRADING_DIR/pyproject.toml; run sync_to_box.sh first" >&2
  exit 1
fi
if [[ ! -f "$SCRIPT_DIR/install_trading_project.sh" ]]; then
  echo "missing trading project installer: $SCRIPT_DIR/install_trading_project.sh" >&2
  exit 1
fi
if [[ ! -f "$SCRIPT_DIR/verify_trading_install.py" ]]; then
  echo "missing trading install verifier: $SCRIPT_DIR/verify_trading_install.py" >&2
  exit 1
fi

if [[ ! -d "$TRADING_DIR/sfo_kalshi_quant" ]]; then
  echo "missing trading repo at $TRADING_DIR" >&2
  exit 1
fi

if [[ ! -f "$FORECASTER_DIR/google_weather_cache.py" ]]; then
  echo "missing forecaster repo at $FORECASTER_DIR" >&2
  exit 1
fi

TARGET_TIMEZONE="America/Los_Angeles"
if CURRENT_TIMEZONE="$(timedatectl show -p Timezone --value)"; then
  :
else
  status=$?
  echo "failed to read host timezone; no changes made" >&2
  exit "$status"
fi
if [[ "$CURRENT_TIMEZONE" != "$TARGET_TIMEZONE" ]]; then
  echo "host timezone is $CURRENT_TIMEZONE, expected $TARGET_TIMEZONE; use install_systemd_notimers.sh for a safe timezone cutover" >&2
  exit 1
fi
if [[ ! -f "$BASE_DIR/requirements/production.lock" ]]; then
  echo "missing hashed production lock at $BASE_DIR/requirements/production.lock" >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y curl git python3 python3-venv python3-pip sqlite3 rsync

mkdir -p "$TRADING_DIR/data" "$TRADING_DIR/logs" "$FORECASTER_DIR/logs"

if [[ ! -d "$TRADING_DIR/.venv" ]]; then
  "$WEATHEREDGE_VENV_PYTHON" -m venv "$TRADING_DIR/.venv"
fi
APP_GROUP="${APP_GROUP:-$(id -gn "$APP_USER" 2>/dev/null || printf '%s' "$APP_USER")}"
sudo chown -R "$APP_USER:$APP_GROUP" "$TRADING_DIR/.venv"
"$TRADING_DIR/.venv/bin/python" -m pip install \
  --require-hashes -r "$BASE_DIR/requirements/production.lock"
bash "$SCRIPT_DIR/install_trading_project.sh" "$BASE_DIR" "$TRADING_DIR/.venv/bin/python"

if [[ ! -d "$FORECASTER_DIR/.venv" ]]; then
  python3 -m venv "$FORECASTER_DIR/.venv"
fi
"$FORECASTER_DIR/.venv/bin/python" -m pip install \
  --require-hashes -r "$BASE_DIR/requirements/production.lock"

if [[ ! -f "$ENV_FILE" ]]; then
  sudo install -m 600 "$SCRIPT_DIR/sfo-weather.env.example" "$ENV_FILE"
  echo "created $ENV_FILE"
fi
sudo install -d -m 755 /usr/local/libexec/weatheredge
sudo install -m 755 "$SCRIPT_DIR/migrate_weatheredge_env.py" "/usr/local/libexec/weatheredge/migrate_weatheredge_env.py"

# Migrate only the superseded publication defaults. Preserve any operator-set
# custom thresholds rather than replacing the environment file wholesale.
if sudo grep -qx "SFO_PUBLICATION_MAX_OPERATIONAL_AGE_MINUTES=15" "$ENV_FILE"; then
  sudo sed -i \
    "s/^SFO_PUBLICATION_MAX_OPERATIONAL_AGE_MINUTES=15$/SFO_PUBLICATION_MAX_OPERATIONAL_AGE_MINUTES=10/" \
    "$ENV_FILE"
fi
if sudo grep -qx "SFO_PUBLICATION_MAX_PUBLIC_OPERATIONAL_AGE_MINUTES=10" "$ENV_FILE"; then
  sudo sed -i \
    "s/^SFO_PUBLICATION_MAX_PUBLIC_OPERATIONAL_AGE_MINUTES=10$/SFO_PUBLICATION_MAX_PUBLIC_OPERATIONAL_AGE_MINUTES=20/" \
    "$ENV_FILE"
fi
if sudo grep -qx "SFO_SCHEDULER_PROPAGATION_TIMEOUT_SECONDS=180" "$ENV_FILE"; then
  sudo sed -i \
    "s/^SFO_SCHEDULER_PROPAGATION_TIMEOUT_SECONDS=180$/SFO_SCHEDULER_PROPAGATION_TIMEOUT_SECONDS=420/" \
    "$ENV_FILE"
fi
sudo /usr/bin/python3 -I "/usr/local/libexec/weatheredge/migrate_weatheredge_env.py" "$ENV_FILE"

render_unit() {
  local src="$1"
  local dst="$2"
  sed \
    -e "s#__APP_USER__#$APP_USER#g" \
    -e "s#__APP_GROUP__#$APP_GROUP#g" \
    -e "s#__TRADING_DIR__#$TRADING_DIR#g" \
    -e "s#__FORECASTER_DIR__#$FORECASTER_DIR#g" \
    -e "s#__ENV_FILE__#$ENV_FILE#g" \
    "$src" | sudo tee "$dst" >/dev/null
}

render_unit "$SCRIPT_DIR/systemd/sfo-forecaster-refresh.service.in" /etc/systemd/system/sfo-forecaster-refresh.service
render_unit "$SCRIPT_DIR/systemd/weatheredge-google-nonsfo-refresh.service.in" /etc/systemd/system/weatheredge-google-nonsfo-refresh.service
render_unit "$SCRIPT_DIR/systemd/weatheredge-apple-refresh.service.in" /etc/systemd/system/weatheredge-apple-refresh.service
render_unit "$SCRIPT_DIR/systemd/weatheredge-apple-purge.service.in" /etc/systemd/system/weatheredge-apple-purge.service
render_unit "$SCRIPT_DIR/systemd/weatheredge-google-runtime-purge.service.in" /etc/systemd/system/weatheredge-google-runtime-purge.service
render_unit "$SCRIPT_DIR/systemd/sfo-operational-publish.service.in" /etc/systemd/system/sfo-operational-publish.service
render_unit "$SCRIPT_DIR/systemd/sfo-strategy-lab-refresh.service.in" /etc/systemd/system/sfo-strategy-lab-refresh.service
render_unit "$SCRIPT_DIR/systemd/sfo-dataset-backfill.service.in" /etc/systemd/system/sfo-dataset-backfill.service
render_unit "$SCRIPT_DIR/systemd/sfo-kalshi-paper-scan.service.in" /etc/systemd/system/sfo-kalshi-paper-scan.service
render_unit "$SCRIPT_DIR/systemd/sfo-kalshi-paper-monitor.service.in" /etc/systemd/system/sfo-kalshi-paper-monitor.service
render_unit "$SCRIPT_DIR/systemd/sfo-kalshi-paper-settle.service.in" /etc/systemd/system/sfo-kalshi-paper-settle.service
render_unit "$SCRIPT_DIR/systemd/sfo-kalshi-paper-prune.service.in" /etc/systemd/system/sfo-kalshi-paper-prune.service
render_unit "$SCRIPT_DIR/systemd/sfo-forecast-freshness.service.in" /etc/systemd/system/sfo-forecast-freshness.service
render_unit "$SCRIPT_DIR/systemd/sfo-scheduler-health.service.in" /etc/systemd/system/sfo-scheduler-health.service
render_unit "$SCRIPT_DIR/systemd/sfo-alert@.service.in" /etc/systemd/system/sfo-alert@.service

chmod +x "$SCRIPT_DIR/check_forecast_db_freshness.sh" "$SCRIPT_DIR/wait_for_publication_manifest.sh" "$SCRIPT_DIR/send_systemd_failure_alert.sh" 2>/dev/null || true
sudo install -m 755 "$SCRIPT_DIR/check_scheduler_health.sh" /usr/local/libexec/weatheredge/check_scheduler_health.sh
sudo install -m 755 "$SCRIPT_DIR/verify_systemd_unit_integrity.sh" /usr/local/libexec/weatheredge/verify_systemd_unit_integrity.sh

# Task 8 item 1: /run/weatheredge is created, owned, and permission-enforced
# by a static tmpfiles.d entry rather than a per-unit RuntimeDirectory=,
# because multiple independent units (the SFO refresh, non-SFO refresh, Apple
# refresh, and provider purge) share the private runtime directory and
# RuntimeDirectory= ties a directory's lifecycle to a single owning unit.
# `--create` applies it immediately so a fresh install does not have to wait
# for a reboot before the first provider refresh can open its runtime store.
render_unit "$SCRIPT_DIR/systemd/weatheredge-tmpfiles.conf" /etc/tmpfiles.d/weatheredge.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/weatheredge.conf

sudo install -m 644 "$SCRIPT_DIR/systemd/sfo-forecaster-refresh.timer" /etc/systemd/system/sfo-forecaster-refresh.timer
sudo install -m 644 "$SCRIPT_DIR/systemd/weatheredge-google-nonsfo-refresh.timer" /etc/systemd/system/weatheredge-google-nonsfo-refresh.timer
sudo install -m 644 "$SCRIPT_DIR/systemd/weatheredge-apple-refresh.timer" /etc/systemd/system/weatheredge-apple-refresh.timer
sudo install -m 644 "$SCRIPT_DIR/systemd/weatheredge-apple-purge.timer" /etc/systemd/system/weatheredge-apple-purge.timer
sudo install -m 644 "$SCRIPT_DIR/systemd/weatheredge-google-runtime-purge.timer" /etc/systemd/system/weatheredge-google-runtime-purge.timer
sudo install -m 644 "$SCRIPT_DIR/systemd/sfo-operational-publish.timer" /etc/systemd/system/sfo-operational-publish.timer
sudo install -m 644 "$SCRIPT_DIR/systemd/sfo-strategy-lab-refresh.timer" /etc/systemd/system/sfo-strategy-lab-refresh.timer
sudo install -m 644 "$SCRIPT_DIR/systemd/sfo-dataset-backfill.timer" /etc/systemd/system/sfo-dataset-backfill.timer
sudo install -m 644 "$SCRIPT_DIR/systemd/sfo-kalshi-paper-scan.timer" /etc/systemd/system/sfo-kalshi-paper-scan.timer
sudo install -m 644 "$SCRIPT_DIR/systemd/sfo-kalshi-paper-monitor.timer" /etc/systemd/system/sfo-kalshi-paper-monitor.timer
sudo install -m 644 "$SCRIPT_DIR/systemd/sfo-kalshi-paper-settle.timer" /etc/systemd/system/sfo-kalshi-paper-settle.timer
sudo install -m 644 "$SCRIPT_DIR/systemd/sfo-kalshi-paper-prune.timer" /etc/systemd/system/sfo-kalshi-paper-prune.timer
sudo install -m 644 "$SCRIPT_DIR/systemd/sfo-forecast-freshness.timer" /etc/systemd/system/sfo-forecast-freshness.timer
sudo install -m 644 "$SCRIPT_DIR/systemd/sfo-scheduler-health.timer" /etc/systemd/system/sfo-scheduler-health.timer

sudo systemctl daemon-reload

if sudo grep -q "replace_with_google_weather_key" "$ENV_FILE"; then
  echo "Edit $ENV_FILE and set GOOGLE_WEATHER_API_KEY before enabling timers."
  echo "Then run:"
  echo "  sudo systemctl enable --now sfo-forecaster-refresh.timer weatheredge-google-nonsfo-refresh.timer weatheredge-apple-refresh.timer weatheredge-apple-purge.timer weatheredge-google-runtime-purge.timer sfo-operational-publish.timer sfo-strategy-lab-refresh.timer sfo-dataset-backfill.timer sfo-kalshi-paper-scan.timer sfo-kalshi-paper-monitor.timer sfo-kalshi-paper-settle.timer sfo-kalshi-paper-prune.timer sfo-forecast-freshness.timer sfo-scheduler-health.timer"
  exit 0
fi

sudo systemctl enable --now sfo-forecaster-refresh.timer weatheredge-google-nonsfo-refresh.timer weatheredge-apple-refresh.timer weatheredge-apple-purge.timer weatheredge-google-runtime-purge.timer sfo-operational-publish.timer sfo-strategy-lab-refresh.timer sfo-dataset-backfill.timer sfo-kalshi-paper-scan.timer sfo-kalshi-paper-monitor.timer sfo-kalshi-paper-settle.timer sfo-kalshi-paper-prune.timer sfo-forecast-freshness.timer sfo-scheduler-health.timer
sudo systemctl list-timers 'sfo-*' 'weatheredge-*' --all
