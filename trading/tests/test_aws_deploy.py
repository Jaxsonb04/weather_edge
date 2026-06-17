from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AWS_DIR = ROOT / "trading" / "deploy" / "aws"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_systemd_units_use_rendered_weatheredge_env_file():
    installer = _read(AWS_DIR / "install_systemd.sh")
    assert "ENV_FILE=\"${ENV_FILE:-/etc/weatheredge.env}\"" in installer
    assert "s#__ENV_FILE__#$ENV_FILE#g" in installer

    for unit in (AWS_DIR / "systemd").glob("*.service.in"):
        text = _read(unit)
        assert "EnvironmentFile=__ENV_FILE__" in text
        assert "/etc/sfo-weather.env" not in text


def test_forecaster_refresh_generates_signal_before_dashboard_publish():
    text = _read(AWS_DIR / "systemd" / "sfo-forecaster-refresh.service.in")
    signal_idx = text.index("build_public_trading_signal.sh")
    dashboard_idx = text.index("build_dashboard.py")
    publish_idx = text.index("publish_forecaster_pages.sh")
    assert signal_idx < dashboard_idx < publish_idx


def test_strategy_lab_refresh_timer_is_installed_and_avoids_google_refresh():
    installer = _read(AWS_DIR / "install_systemd.sh")
    service = _read(AWS_DIR / "systemd" / "sfo-strategy-lab-refresh.service.in")
    timer = _read(AWS_DIR / "systemd" / "sfo-strategy-lab-refresh.timer")

    assert "sfo-strategy-lab-refresh.service.in" in installer
    assert "sfo-strategy-lab-refresh.timer" in installer
    assert "sfo-strategy-lab-refresh.timer" in installer
    assert "build_public_trading_signal.sh" in service
    assert "build_dashboard.py" in service
    assert "publish_forecaster_pages.sh" in service
    assert "google_weather_cache.py --refresh" not in service
    assert "OnUnitActiveSec=5min" in timer
    assert "Unit=sfo-strategy-lab-refresh.service" in timer


def test_public_signal_builder_is_read_only_and_paper_only():
    text = _read(AWS_DIR / "build_public_trading_signal.sh")
    assert "daily-report" in text
    assert "strategy-research" in text
    assert "command -v" in text
    assert "--no-live-market" not in text
    assert "SFO_TRADING_SIGNAL_CALIBRATION_SOURCE:-lstm" in text
    assert "--calibration-source" in text
    assert "--output" in text
    assert "--place-paper" not in text
    assert "paper-buy" not in text


def test_paper_scan_pins_calibration_source():
    service = _read(AWS_DIR / "systemd" / "sfo-kalshi-paper-scan.service.in")
    runner = _read(AWS_DIR / "run_paper_scan_profiles.sh")
    example_env = _read(AWS_DIR / "sfo-weather.env.example")

    assert "run_paper_scan_profiles.sh" in service
    assert 'CALIBRATION_SOURCE="${SFO_TRADING_SIGNAL_CALIBRATION_SOURCE:-lstm}"' in runner
    assert 'PAPER_ENTRY_MODE="${PAPER_ENTRY_MODE:-market}"' in runner
    assert '--calibration-source "$CALIBRATION_SOURCE"' in runner
    assert '--paper-entry-mode "$PAPER_ENTRY_MODE"' in runner
    assert 'TARGET_DATE="${SFO_PAPER_SCAN_TARGET_DATE:-rolling}"' in runner
    assert 'TAIL_BASKET_ENABLED="${SFO_PAPER_SCAN_TAIL_BASKET_ENABLED:-1}"' in runner
    assert "tail-basket" in runner
    assert "--max-worst-case-loss" in runner
    assert "PAPER_RISK_PROFILES=balanced,fast-feedback" in example_env
    # The deployment example uses market entry (2026-06-17) so approved scans
    # fill immediately at the ask instead of resting as limit orders that expire
    # unfilled; the runner also defaults to market when unset (asserted above).
    assert "PAPER_ENTRY_MODE=market" in example_env
    assert "SFO_PAPER_SCAN_TAIL_BASKET_ENABLED=1" in example_env
    assert "SFO_TAIL_BASKET_TAIL_STAKE=5" in example_env
    assert "SFO_TAIL_BASKET_CENTER_STAKE=1" in example_env


def test_paper_trading_timers_run_around_the_clock_and_auto_settle():
    scan = _read(AWS_DIR / "systemd" / "sfo-kalshi-paper-scan.timer")
    monitor = _read(AWS_DIR / "systemd" / "sfo-kalshi-paper-monitor.timer")
    settle = _read(AWS_DIR / "systemd" / "sfo-kalshi-paper-settle.timer")
    installer = _read(AWS_DIR / "install_systemd.sh")

    assert "OnCalendar=*-*-* *:00,15,30,45" in scan
    assert "OnCalendar=*-*-* *:01,03,05,07,09,11,13,15,17,19,21,23,25,27,29,31,33,35,37,39,41,43,45,47,49,51,53,55,57,59" in monitor
    assert "OnCalendar=*-*-* *:10,40" in settle
    assert "sfo-kalshi-paper-settle.service.in" in installer
    assert "sfo-kalshi-paper-settle.timer" in installer


def test_paper_monitor_service_uses_side_aware_exit_env():
    service = _read(AWS_DIR / "systemd" / "sfo-kalshi-paper-monitor.service.in")
    example_env = _read(AWS_DIR / "sfo-weather.env.example")

    assert "--yes-take-profit-pct ${PAPER_YES_TAKE_PROFIT_PCT}" in service
    assert "--yes-stop-loss-pct ${PAPER_YES_STOP_LOSS_PCT}" in service
    assert "--no-take-profit-pct ${PAPER_NO_TAKE_PROFIT_PCT}" in service
    assert "--no-stop-loss-pct ${PAPER_NO_STOP_LOSS_PCT}" in service
    assert "--model-veto-max-loss-pct ${PAPER_MODEL_VETO_MAX_LOSS_PCT}" in service
    assert "--model-veto-buffer ${PAPER_MODEL_VETO_BUFFER}" in service
    assert "PAPER_YES_STOP_LOSS_PCT=25" in example_env
    assert "PAPER_MODEL_VETO_MAX_LOSS_PCT=60" in example_env
    assert "PAPER_MODEL_VETO_BUFFER=0.08" in example_env


def test_dataset_backfill_timer_is_lightsail_safe_and_installed():
    installer = _read(AWS_DIR / "install_systemd.sh")
    service = _read(AWS_DIR / "systemd" / "sfo-dataset-backfill.service.in")
    timer = _read(AWS_DIR / "systemd" / "sfo-dataset-backfill.timer")
    runner = _read(AWS_DIR / "run_dataset_backfill.sh")
    example_env = _read(AWS_DIR / "sfo-weather.env.example")

    assert "sfo-dataset-backfill.service.in" in installer
    assert "sfo-dataset-backfill.timer" in installer
    assert "sfo-dataset-backfill.timer" in installer
    assert "run_dataset_backfill.sh" in service
    assert "EnvironmentFile=__ENV_FILE__" in service
    assert "OnCalendar=*-*-* 02:25:00" in timer
    assert "Unit=sfo-dataset-backfill.service" in timer

    assert 'SFO_DATASET_SOURCES="${SFO_DATASET_SOURCES:-iem-asos,open-meteo-previous-runs,open-meteo-historical-forecast,kalshi-history}"' in runner
    default_sources = "SFO_DATASET_SOURCES=iem-asos,open-meteo-previous-runs,open-meteo-historical-forecast,kalshi-history"
    assert default_sources in example_env
    assert "dataset-backfill" in runner
    assert "--source noaa-isd" not in runner
    assert 'SFO_DATASET_DB:-${SFO_KALSHI_DB:-$TRADING_DIR/data/paper_trading.db}' in runner
    assert 'KALSHI_LOOKBACK_DAYS="${SFO_DATASET_KALSHI_LOOKBACK_DAYS:-90}"' in runner
    assert "SFO_DATASET_KALSHI_LOOKBACK_DAYS=90" in example_env
    assert 'SFO_DATASET_KALSHI_CANDLES:-0' in runner
    assert 'SFO_DATASET_KALSHI_TRADES:-0' in runner
    assert "SFO_DATASET_KALSHI_CANDLES=0" in example_env
    assert "SFO_DATASET_KALSHI_TRADES=0" in example_env
    assert '${1,,}' not in runner
    assert "tr '[:upper:]' '[:lower:]'" in runner


def test_pages_publish_includes_generated_detail_page():
    publisher = _read(AWS_DIR / "publish_forecaster_pages.sh")
    syncer = _read(AWS_DIR / "sync_forecaster_source.sh")
    example_env = _read(AWS_DIR / "sfo-weather.env.example")

    assert "index.html" in publisher
    assert "details.html" in publisher
    assert "strategy-lab.html" in publisher
    assert "strategy_research.json" in publisher
    assert "strategy_research.protected.json" in publisher
    assert "SFO_STRATEGY_LAB_PASSWORD" in publisher
    assert "SFO_STRATEGY_LAB_PUBLIC_MODE" in publisher
    assert "SFO_STRATEGY_LAB_PUBLIC_MODE=1" in example_env
    assert "SFO_PAGES_GIT_AUTHOR_NAME=JaxsonB04" in example_env
    assert "SFO_PAGES_GIT_AUTHOR_EMAIL=JaxsonB04@users.noreply.github.com" in example_env
    assert '${SFO_PAGES_GIT_AUTHOR_NAME:-JaxsonB04}' in publisher
    assert '${SFO_PAGES_GIT_AUTHOR_EMAIL:-JaxsonB04@users.noreply.github.com}' in publisher
    assert '--exclude "/index.html"' in syncer
    assert '--exclude "/details.html"' in syncer
    assert '--exclude "/strategy-lab.html"' in syncer
    assert '--exclude "strategy_research.json"' in syncer
    assert '--exclude "strategy_research.protected.json"' in syncer


def test_pages_deploy_key_path_matches_lightsail_setup_docs():
    example_env = _read(AWS_DIR / "sfo-weather.env.example")
    publisher = _read(AWS_DIR / "publish_forecaster_pages.sh")
    syncer = _read(AWS_DIR / "sync_forecaster_source.sh")
    readme = _read(AWS_DIR / "README.md")

    expected = "sfo_weather_pages_deploy"
    assert expected in example_env
    assert expected in publisher
    assert expected in syncer
    assert expected in readme
    assert "weatheredge_pages_deploy" not in example_env + publisher + syncer + readme


def test_initial_lightsail_sync_does_not_copy_local_runtime_state():
    syncer = _read(AWS_DIR / "sync_to_lightsail.sh")

    assert syncer.count("--exclude '.pytest_cache'") == 2

    for artifact in (
        "weather.db",
        "*.db-journal",
        "*.sqlite",
        "*.sqlite3",
        "google_weather_cache.json",
        "trading_signal.json",
        "strategy_research.json",
        "strategy_research.protected.json",
    ):
        assert f"--exclude '{artifact}'" in syncer

    for artifact in (
        "/index.html",
        "/details.html",
        "/strategy-lab.html",
    ):
        assert f"--exclude '{artifact}'" in syncer
