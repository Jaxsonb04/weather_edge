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


def test_installer_forecaster_venv_installs_runtime_dependencies():
    installer = _read(AWS_DIR / "install_systemd.sh")

    assert '"$FORECASTER_DIR/.venv/bin/python" -m pip install certifi numpy pandas' in installer


def test_github_verify_workflow_installs_test_import_dependencies():
    workflow = _read(ROOT / ".github" / "workflows" / "verify.yml")

    assert "python -m pip install -e '.[dev]'" in workflow
    assert "semgrep" in workflow


def test_forecaster_refresh_generates_signal_before_publish():
    text = _read(AWS_DIR / "systemd" / "sfo-forecaster-refresh.service.in")
    signal_idx = text.index("build_public_trading_signal.sh")
    publish_idx = text.index("publish_forecaster_pages.sh")
    assert signal_idx < publish_idx
    # The legacy generated-HTML dashboard is retired; the SPA in webdist is
    # the only site the publisher ships.
    assert "build_dashboard.py" not in text


def test_strategy_lab_refresh_timer_is_installed_and_avoids_google_refresh():
    installer = _read(AWS_DIR / "install_systemd.sh")
    service = _read(AWS_DIR / "systemd" / "sfo-strategy-lab-refresh.service.in")
    timer = _read(AWS_DIR / "systemd" / "sfo-strategy-lab-refresh.timer")

    assert "sfo-strategy-lab-refresh.service.in" in installer
    assert "sfo-strategy-lab-refresh.timer" in installer
    assert "sfo-strategy-lab-refresh.timer" in installer
    assert "build_public_trading_signal.sh" in service
    assert "build_dashboard.py" not in service
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
    assert '"$PYTHON_BIN" -m sfo_kalshi_quant.cli "${args[@]}" >/dev/null' in text
    assert '--output "$RESEARCH_OUTPUT_PATH" >/dev/null' in text


def test_paper_scan_pins_calibration_source():
    service = _read(AWS_DIR / "systemd" / "sfo-kalshi-paper-scan.service.in")
    runner = _read(AWS_DIR / "run_paper_scan_profiles.sh")
    example_env = _read(AWS_DIR / "sfo-weather.env.example")
    readme = _read(AWS_DIR / "README.md")

    assert "run_paper_scan_profiles.sh" in service
    assert "portfolio-scan" in runner
    assert 'CALIBRATION_SOURCE="${SFO_TRADING_SIGNAL_CALIBRATION_SOURCE:-lstm}"' in runner
    assert 'PAPER_ENTRY_MODE="${PAPER_ENTRY_MODE:-market}"' in runner
    assert '--calibration-source "$CALIBRATION_SOURCE"' in runner
    assert '--paper-entry-mode "$PAPER_ENTRY_MODE"' in runner
    assert 'TARGET_DATE="${SFO_PAPER_SCAN_TARGET_DATE:-rolling}"' in runner
    assert 'PORTFOLIO_MAX_ARB_SPEND="${SFO_PORTFOLIO_MAX_ARB_SPEND:-12}"' in runner
    assert 'PORTFOLIO_MIN_PROFIT="${SFO_PORTFOLIO_MIN_PROFIT:-0.01}"' in runner
    assert "PAPER_RISK_PROFILES=live,research" in example_env
    # The deployment example uses market entry (2026-06-17) so approved scans
    # fill immediately at the ask instead of resting as limit orders that expire
    # unfilled; the runner also defaults to market when unset (asserted above).
    assert "PAPER_ENTRY_MODE=market" in example_env
    assert "SFO_PORTFOLIO_MAX_ARB_SPEND=12" in example_env
    assert "SFO_PORTFOLIO_MIN_PROFIT=0.01" in example_env
    assert "balanced,fast-feedback,exploratory" not in example_env
    assert "balanced,fast-feedback,exploratory" not in readme


def test_paper_trading_timers_run_around_the_clock_and_auto_settle():
    scan = _read(AWS_DIR / "systemd" / "sfo-kalshi-paper-scan.timer")
    monitor = _read(AWS_DIR / "systemd" / "sfo-kalshi-paper-monitor.timer")
    settle = _read(AWS_DIR / "systemd" / "sfo-kalshi-paper-settle.timer")
    installer = _read(AWS_DIR / "install_systemd.sh")

    assert "OnCalendar=*-*-* *:00,05,10,15,20,25,30,35,40,45,50,55" in scan
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

    assert 'SFO_DATASET_SOURCES="${SFO_DATASET_SOURCES:-iem-asos,open-meteo-previous-runs,open-meteo-historical-forecast,lamp,gfs-mos,nbm,hrrr,kalshi-history}"' in runner
    default_sources = "SFO_DATASET_SOURCES=iem-asos,open-meteo-previous-runs,open-meteo-historical-forecast,lamp,gfs-mos,nbm,hrrr,kalshi-history"
    assert default_sources in example_env
    assert "dataset-backfill" in runner
    assert "--source noaa-isd" not in runner
    assert 'SFO_DATASET_DB:-${SFO_KALSHI_DB:-$TRADING_DIR/data/paper_trading.db}' in runner
    assert "failed_sources=()" in runner
    assert "failed; continuing" in runner
    assert 'KALSHI_LOOKBACK_DAYS="${SFO_DATASET_KALSHI_LOOKBACK_DAYS:-90}"' in runner
    assert "SFO_DATASET_KALSHI_LOOKBACK_DAYS=90" in example_env
    assert 'SFO_DATASET_KALSHI_CANDLES:-0' in runner
    assert 'SFO_DATASET_KALSHI_TRADES:-0' in runner
    assert "SFO_DATASET_KALSHI_CANDLES=0" in example_env
    assert "SFO_DATASET_KALSHI_TRADES=0" in example_env
    assert '${1,,}' not in runner
    assert "tr '[:upper:]' '[:lower:]'" in runner


def test_pages_publish_ships_spa_and_fresh_jsons():
    publisher = _read(AWS_DIR / "publish_forecaster_pages.sh")
    syncer = _read(AWS_DIR / "sync_forecaster_source.sh")
    example_env = _read(AWS_DIR / "sfo-weather.env.example")

    # The site is the prebuilt SPA plus the fresh public research JSONs.
    assert "WEBDIST_DIR" in publisher
    assert "trading_signal.json" in publisher
    assert "forecast_data.json" in publisher
    assert "weather_story_data.json" in publisher
    assert "strategy_research.json" in publisher
    # The legacy generated-HTML/protected pipeline is retired.
    assert "strategy_research.protected.json" not in publisher
    assert "SFO_STRATEGY_LAB_PUBLIC_MODE" not in publisher
    assert "SFO_PAGES_GIT_AUTHOR_NAME=JaxsonB04" in example_env
    assert "SFO_PAGES_GIT_AUTHOR_EMAIL=JaxsonB04@users.noreply.github.com" in example_env
    assert '${SFO_PAGES_GIT_AUTHOR_NAME:-JaxsonB04}' in publisher
    assert '${SFO_PAGES_GIT_AUTHOR_EMAIL:-JaxsonB04@users.noreply.github.com}' in publisher
    assert '--exclude "strategy_research.json"' in syncer


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


def test_source_sync_serializes_shared_git_cache_and_uses_current_remote():
    syncer = _read(AWS_DIR / "sync_forecaster_source.sh")
    example_env = _read(AWS_DIR / "sfo-weather.env.example")
    readme = _read(AWS_DIR / "README.md")

    assert "weather_edge.git" in syncer
    assert "weather-edge.git" not in syncer
    assert "weather_edge.git" in example_env
    assert "weather-edge.git" not in example_env
    assert "weather_edge.git" in readme
    assert "weather-edge.git" not in readme
    assert "SFO_FORECASTER_SOURCE_LOCK" in syncer
    assert "/opt/weatheredge/.locks/source-cache-main.lock" in syncer
    assert 'mkdir -p "$(dirname "$SOURCE_LOCK")"' in syncer
    assert "flock" in syncer
    assert "exec 9>" in syncer
    assert "git remote set-url origin" in syncer


def test_pages_publish_is_race_safe():
    # Two timers (hourly forecaster-refresh + 5-minute strategy-lab-refresh) run
    # the same publish script, so it must serialize (flock) AND survive a
    # non-fast-forward rejection with a bounded re-fetch/retry loop.
    publisher = _read(AWS_DIR / "publish_forecaster_pages.sh")
    assert "flock" in publisher
    assert "SFO_PAGES_PUSH_ATTEMPTS" in publisher
    assert "re-fetching" in publisher  # the retry path re-fetches the fresh tip


def test_paper_scan_is_overlap_guarded_and_portfolio_allocated():
    runner = _read(AWS_DIR / "run_paper_scan_profiles.sh")
    example_env = _read(AWS_DIR / "sfo-weather.env.example")

    # Overlap guard: a slow scan must not be double-run by the 5-minute timer.
    assert "SFO_PAPER_SCAN_LOCK" in runner
    assert "flock -n" in runner
    assert runner.count("    portfolio-scan") == 1
    assert "tail-basket" not in runner
    assert " arbitrage" not in runner
    assert " analyze" not in runner
    assert "SFO_PORTFOLIO_MAX_ARB_SPEND=12" in example_env


def test_pull_paper_db_script_exists_for_offline_rescore():
    # The readiness rescore needs the live journal locally; sync_to_lightsail.sh
    # only pushes OUT and excludes the DB, so a dedicated inbound pull must exist.
    puller = _read(AWS_DIR / "pull_paper_db.sh")
    assert "paper_trading.db" in puller
    assert "rsync" in puller
    assert "backtest-rescore" in puller  # documents the next step


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
    ):
        assert f"--exclude '{artifact}'" in syncer
