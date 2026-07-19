from __future__ import annotations

import re
import subprocess
from pathlib import Path

from cities import CITIES, DEFAULT_CITY_SLUG


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

    assert '--require-hashes -r "$BASE_DIR/requirements/production.lock"' in installer
    assert "pip install --upgrade" not in installer
    apt_install = next(line for line in installer.splitlines() if "apt-get install" in line)
    assert "curl" in apt_install.split()
    assert "awscli" not in apt_install.split()


def test_installers_repair_trading_venv_ownership_before_project_install():
    for name in ("install_systemd.sh", "install_systemd_notimers.sh"):
        installer = _read(AWS_DIR / name)
        ownership_idx = installer.index('chown -R "$APP_USER:$APP_GROUP" "$TRADING_DIR/.venv"')
        project_install_idx = installer.index('bash "$SCRIPT_DIR/install_trading_project.sh"')
        assert ownership_idx < project_install_idx


def test_backup_provisioner_enforces_bucket_controls_and_least_privilege_prefixes():
    provisioner = _read(AWS_DIR / "provision_backup_bucket.sh")

    assert "put-public-access-block" in provisioner
    assert "put-bucket-versioning" in provisioner
    assert "put-bucket-encryption" in provisioner
    assert "put-bucket-lifecycle-configuration" in provisioner
    assert "paper_trading/*" in provisioner
    assert "database-snapshots/*" in provisioner
    assert "iam put-role-policy" in provisioner
    assert "s3:*" not in provisioner


def test_github_verify_workflow_installs_test_import_dependencies():
    workflow = _read(ROOT / ".github" / "workflows" / "verify.yml")

    assert "python -m pip install --require-hashes -r requirements/production.lock" in workflow
    assert "python -m pip install --no-build-isolation --no-deps -e ." in workflow
    assert "semgrep==" in workflow
    assert 'HEROUI_KEY: ${{ secrets.HEROUI_KEY }}' in workflow
    assert 'if [[ -z "$HEROUI_KEY" ]]' in workflow
    assert "missing from the GitHub Actions secret store" in workflow
    assert "env -u CI npx -y hpsetup@4.7.0 --auto" in workflow


def test_forecaster_refresh_only_refreshes_forecast_state():
    text = _read(AWS_DIR / "systemd" / "sfo-forecaster-refresh.service.in")
    assert "sync_forecaster_source.sh" in text
    assert "nws_ground_truth.py" in text
    assert "google_weather_cache.py" in text
    assert "build_public_trading_signal.sh" not in text
    assert "build_strategy_research.sh" not in text
    assert "publish_forecaster_pages.sh" not in text


def test_sfo_refresh_unit_uses_the_cities_sfo_orchestrator_not_legacy_flags():
    """T8-2 (Task 6 review): the legacy raw-writing `--refresh` SFO-only
    fetch and the separate legacy no-flag compatibility-JSON rebuild must
    never run alongside the new city-aware orchestrator -- exactly one
    `google_weather_cache.py` invocation remains, using `--cities sfo`.
    """
    text = _read(AWS_DIR / "systemd" / "sfo-forecaster-refresh.service.in")

    assert "google_weather_cache.py --cities sfo" in text
    assert "google_weather_cache.py --refresh" not in text
    assert text.count("google_weather_cache.py") == 1


def test_sfo_refresh_unit_does_not_duplicate_the_emos_baseline_serve():
    """T8-3 (Task 6 review): `google_multicity_refresh._archive_baseline_first`
    already runs `emos_forecast.py --serve-rolling --cities all` as a
    subprocess BEFORE any Google fetch is attempted, every time
    `google_weather_cache.py --cities ...` runs. A standalone
    `emos_forecast.py` ExecStart in this same unit would call the identical
    Open-Meteo serve-rolling pipeline a second time every cycle.
    """
    text = _read(AWS_DIR / "systemd" / "sfo-forecaster-refresh.service.in")
    exec_lines = [line for line in text.splitlines() if line.startswith("ExecStart=")]

    assert not any("emos_forecast.py" in line for line in exec_lines)


def test_forecaster_refresh_updates_generic_truth_before_the_cities_orchestrator():
    text = _read(AWS_DIR / "systemd" / "sfo-forecaster-refresh.service.in")
    truth_refresh = (
        "ExecStart=-__FORECASTER_DIR__/.venv/bin/python "
        "__FORECASTER_DIR__/city_truth.py --db __FORECASTER_DIR__/weather.db "
        "--refresh --cities all"
    )
    orchestrator_call = "google_weather_cache.py --cities sfo"

    assert truth_refresh in text
    assert text.index(truth_refresh) < text.index(orchestrator_call)


def test_operational_publish_service_runs_fast_builder_then_publisher():
    installer = _read(AWS_DIR / "install_systemd.sh")
    service = _read(AWS_DIR / "systemd" / "sfo-operational-publish.service.in")
    timer = _read(AWS_DIR / "systemd" / "sfo-operational-publish.timer")

    assert "sfo-operational-publish.service.in" in installer
    assert "sfo-operational-publish.timer" in installer
    assert "sfo-operational-publish.timer" in installer
    assert "sync_forecaster_source.sh" in service
    assert "run_publication_cycle.sh operational" in service
    assert "google_weather_cache.py --refresh" not in service
    assert "OnActiveSec=2min" in timer
    assert "OnBootSec=" not in timer
    assert "OnUnitActiveSec=5min" in timer
    assert "Unit=sfo-operational-publish.service" in timer


def test_web_app_deploy_triggers_fast_operational_publication():
    deployer = _read(AWS_DIR / "deploy_web_app.sh")

    assert "systemctl start sfo-operational-publish.service" in deployer
    assert "systemctl start sfo-strategy-lab-refresh.service" not in deployer


def test_strategy_lab_refresh_runs_only_heavy_builder_every_fifteen_minutes():
    installer = _read(AWS_DIR / "install_systemd.sh")
    service = _read(AWS_DIR / "systemd" / "sfo-strategy-lab-refresh.service.in")
    timer = _read(AWS_DIR / "systemd" / "sfo-strategy-lab-refresh.timer")

    assert "sfo-strategy-lab-refresh.service.in" in installer
    assert "sfo-strategy-lab-refresh.timer" in installer
    assert "sfo-strategy-lab-refresh.timer" in installer
    assert "run_publication_cycle.sh strategy" in service
    assert "build_public_trading_signal.sh" not in service
    assert "google_weather_cache.py --refresh" not in service
    assert "OnActiveSec=4min" in timer
    assert "OnBootSec=" not in timer
    assert "OnUnitActiveSec=15min" in timer
    assert "Unit=sfo-strategy-lab-refresh.service" in timer


def test_operational_builder_generates_fast_artifacts_and_manifest_only():
    text = _read(AWS_DIR / "build_public_trading_signal.sh")
    assert "daily-report" in text
    assert "sfo_kalshi_quant.cities_report" in text
    assert "sfo_kalshi_quant.publication build" in text
    assert "command -v" in text
    assert "--no-live-market" not in text
    assert "SFO_TRADING_SIGNAL_CALIBRATION_SOURCE:-lstm" in text
    assert "--calibration-source" in text
    assert "--output" in text
    assert "--place-paper" not in text
    assert "paper-buy" not in text
    assert '"$PYTHON_BIN" -m sfo_kalshi_quant.cli "${args[@]}" >/dev/null' in text
    assert "strategy-research" not in text


def test_strategy_builder_generates_only_strategy_research():
    text = _read(AWS_DIR / "build_strategy_research.sh")
    assert "strategy-research" in text
    assert "SFO_STRATEGY_RESEARCH_CALIBRATION_MIN_TRAIN:-180" in text
    assert "daily-report" not in text
    assert "sfo_kalshi_quant.cities_report" not in text
    assert "sfo_kalshi_quant.publication build" not in text


def test_publication_cycles_serialize_builder_and_publisher_under_shared_lock():
    runner = _read(AWS_DIR / "run_publication_cycle.sh")
    example_env = _read(AWS_DIR / "sfo-weather.env.example")

    assert 'SFO_ARTIFACT_GENERATION_LOCK:-/opt/weatheredge/.locks/artifact-generation.lock' in runner
    assert "flock" in runner
    assert "SFO_ARTIFACT_LOCK_HELD=1" in runner
    assert "build_public_trading_signal.sh" in runner
    assert "build_strategy_research.sh" in runner
    assert "publish_forecaster_pages.sh" in runner
    assert runner.index("build_public_trading_signal.sh") < runner.index("publish_forecaster_pages.sh")
    assert "SFO_ARTIFACT_GENERATION_LOCK=/opt/weatheredge/.locks/artifact-generation.lock" in example_env


def test_publication_cycle_hands_generation_lock_to_snapshot_copy_then_releases_before_network():
    runner = _read(AWS_DIR / "run_publication_cycle.sh")
    publisher = _read(AWS_DIR / "publish_forecaster_pages.sh")

    held_idx = runner.index("export SFO_ARTIFACT_LOCK_HELD=1")
    fd_idx = runner.index("export SFO_ARTIFACT_LOCK_FD=7")
    build_idx = runner.index('/bin/bash "$BUILDER"')
    publish_idx = runner.index('publish_forecaster_pages.sh')
    assert held_idx < fd_idx < build_idx < publish_idx
    assert "flock -u 7" not in runner

    snapshot_copy_idx = publisher.index('cp "$source_path"')
    publisher_unlock_idx = publisher.index('flock -u "$SFO_ARTIFACT_LOCK_FD"')
    close_idx = publisher.index("exec 7>&-")
    unset_idx = publisher.index("unset SFO_ARTIFACT_LOCK_HELD SFO_ARTIFACT_LOCK_FD")
    git_init_idx = publisher.index("git init")
    fetch_idx = publisher.index("git fetch")
    assert snapshot_copy_idx < publisher_unlock_idx < close_idx < unset_idx < git_init_idx < fetch_idx
    assert "exec 8>&-" in publisher


def test_strategy_cycle_rebuilds_manifest_before_publishing_research():
    runner = _read(AWS_DIR / "run_publication_cycle.sh")

    research_idx = runner.index("build_strategy_research.sh")
    manifest_idx = runner.index("sfo_kalshi_quant.publication build")
    publish_idx = runner.index("publish_forecaster_pages.sh")
    assert research_idx < manifest_idx < publish_idx


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
    # Maker-first reorientation (2026-07-06): the deployment example posts
    # resting limit orders (maker fees, favorite-band strategy) and scans every
    # configured city; the runner still defaults to market when unset so ad-hoc
    # local runs stay comparable to the historical taker journal.
    assert "PAPER_ENTRY_MODE=limit" in example_env
    assert "PAPER_CITIES=all" in example_env
    assert "SFO_PAPER_SCAN_LOCK=/opt/weatheredge/.locks/paper-scan.lock" in example_env
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


def test_dataset_backfill_timer_is_production_safe_and_installed():
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
    assert (
        "SFO_DATASET_RESEARCH_PATH=/opt/weatheredge/forecaster/dataset_research.json"
        in example_env
    )
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


def test_paper_prune_unit_is_installed_and_archive_gated():
    installer = _read(AWS_DIR / "install_systemd.sh")
    service = _read(AWS_DIR / "systemd" / "sfo-kalshi-paper-prune.service.in")
    timer = _read(AWS_DIR / "systemd" / "sfo-kalshi-paper-prune.timer")

    assert "sfo-kalshi-paper-prune.service.in" in installer
    assert "sfo-kalshi-paper-prune.timer" in installer
    # Enabled alongside the other timers (echo hint + enable line).
    assert installer.count("sfo-kalshi-paper-settle.timer sfo-kalshi-paper-prune.timer") == 2
    assert "run_archive_then_prune.sh" in service
    assert "EnvironmentFile=__ENV_FILE__" in service
    # The archive-then-prune chain runs long; it must outlive the 90 s default.
    assert "TimeoutStartSec=1800" in service
    assert "OnCalendar=*-*-* 09:20:00 UTC" in timer
    assert "Persistent=true" in timer
    assert "Unit=sfo-kalshi-paper-prune.service" in timer


def test_only_dedicated_service_template_runs_paper_prune():
    service_templates = sorted((AWS_DIR / "systemd").glob("*.service.in"))
    prune_templates = [
        path.name
        for path in service_templates
        if "paper-prune" in _read(path) or "run_archive_then_prune" in _read(path)
    ]

    assert prune_templates == ["sfo-kalshi-paper-prune.service.in"]
    assert not list((AWS_DIR.parents[1] / "sfo_kalshi_quant").glob("*.service.in"))


def test_paper_prune_retention_is_explicit_in_canonical_environment():
    example_env = _read(AWS_DIR / "sfo-weather.env.example")

    assert "SFO_PRUNE_FULL_DAYS=1" in example_env


def test_source_sync_preserves_stale_forecast_watchdog_marker():
    # sync_forecaster_source.sh rsyncs with --delete into the forecaster root,
    # which is also where the freshness watchdog writes its STALE_FORECAST
    # marker; without this exclude the 5-minute sync silently erases the alarm.
    syncer = _read(AWS_DIR / "sync_forecaster_source.sh")
    excludes = _read(AWS_DIR / "forecaster-runtime.rsync-filter")
    assert '--exclude-from="$FORECASTER_EXCLUDES"' in syncer
    assert "STALE_FORECAST" in excludes


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
    excludes = _read(AWS_DIR / "forecaster-runtime.rsync-filter")
    assert "strategy_research.json" in excludes
    assert "cities_data.json" in excludes
    assert "publication_manifest.json" in excludes


def test_forecaster_filter_preserves_build_provenance():
    """build_info.json (audit PR-01) is stamped once by sync_to_box.sh but must
    survive the 5-minute sync_forecaster_source.sh git-tree refresh every
    publish cycle runs, or the provenance-stamped manifest silently reverts to
    unprovenanced on the very next cycle."""

    excludes = _read(AWS_DIR / "forecaster-runtime.rsync-filter")
    assert "build_info.json" in excludes


def test_pages_deploy_key_path_matches_ec2_setup_docs():
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
    # The operational and Strategy Lab timers share the publisher, so it must
    # survive a non-fast-forward rejection with a bounded re-fetch/retry loop.
    publisher = _read(AWS_DIR / "publish_forecaster_pages.sh")
    assert "flock" in publisher
    assert "SFO_PAGES_PUSH_ATTEMPTS" in publisher
    assert "re-fetching" in publisher  # the retry path re-fetches the fresh tip


def test_pages_publisher_validates_manifest_and_copies_exact_validated_artifacts():
    publisher = _read(AWS_DIR / "publish_forecaster_pages.sh")

    for artifact in (
        "trading_signal.json",
        "forecast_data.json",
        "weather_story_data.json",
        "cities_data.json",
        "publication_manifest.json",
    ):
        assert artifact in publisher

    assert "strategy_research.json" in publisher
    assert "--print-artifacts" in publisher
    assert "SFO_REQUIRE_STRATEGY_ARTIFACT" in publisher
    validate_idx = publisher.index("sfo_kalshi_quant.publication validate")
    copy_idx = publisher.index('cp "$source_path"')
    assert validate_idx < copy_idx
    assert 'if [[ -e "$FORECASTER_DIR/$artifact" ]]' not in publisher


def test_strategy_cycle_requires_research_but_operational_cycle_allows_missing():
    runner = _read(AWS_DIR / "run_publication_cycle.sh")
    publisher = _read(AWS_DIR / "publish_forecaster_pages.sh")

    assert "export SFO_REQUIRE_STRATEGY_ARTIFACT=1" in runner
    assert "--require-strategy" in publisher
    assert runner.index("build_strategy_research.sh") < runner.index(
        "export SFO_REQUIRE_STRATEGY_ARTIFACT=1"
    )


def test_pages_publisher_uses_generation_lock_separately_from_git_lock():
    publisher = _read(AWS_DIR / "publish_forecaster_pages.sh")

    assert "SFO_ARTIFACT_GENERATION_LOCK" in publisher
    assert "SFO_ARTIFACT_LOCK_HELD" in publisher
    assert "SFO_PAGES_LOCK" in publisher
    assert "ARTIFACT_LOCK" in publisher
    assert "PAGES_LOCK" in publisher
    assert publisher.index("ARTIFACT_LOCK") < publisher.index("sfo_kalshi_quant.publication validate")


def test_freshness_watchdog_configuration_documents_manifest_thresholds():
    watchdog = _read(AWS_DIR / "check_forecast_db_freshness.sh")
    example_env = _read(AWS_DIR / "sfo-weather.env.example")
    readme = _read(AWS_DIR / "README.md")
    deployment = _read(AWS_DIR.parents[2] / "docs" / "aws_deployment.md")

    assert "sfo_kalshi_quant.publication validate" in watchdog
    assert "SFO_PUBLICATION_MAX_OPERATIONAL_AGE_MINUTES=15" in example_env
    assert "SFO_PUBLICATION_MAX_PUBLIC_OPERATIONAL_AGE_MINUTES=20" in example_env
    assert "SFO_PUBLICATION_MAX_STRATEGY_AGE_MINUTES=20" in example_env
    assert (
        "SFO_PUBLICATION_MANIFEST_URL="
        "https://jaxsonb04.github.io/weather_edge/publication_manifest.json"
    ) in example_env
    assert "shared sfo-alert@.service JSON" in watchdog
    assert "Slack/Discord" not in watchdog
    for documentation in (readme, deployment):
        assert "15 minutes" in documentation
        assert "20 minutes" in documentation
        assert "SFO_PUBLICATION_MANIFEST_URL" in documentation


def test_project_docs_describe_split_publication_cadences():
    root = AWS_DIR.parents[2]
    documentation = (
        _read(root / "forecaster" / "README.md"),
        _read(root / "docs" / "operational_runbook.md"),
    )

    for text in documentation:
        normalized = " ".join(text.split())
        assert "sfo-operational-publish.timer" in normalized
        assert "every five minutes" in normalized
        assert "publication_manifest.json" in normalized
        assert "sfo-strategy-lab-refresh.timer" in normalized
        assert "every fifteen minutes" in normalized
        assert "research-only" in normalized


def test_paper_scan_is_overlap_guarded_and_portfolio_allocated():
    runner = _read(AWS_DIR / "run_paper_scan_profiles.sh")
    example_env = _read(AWS_DIR / "sfo-weather.env.example")
    service = _read(AWS_DIR / "systemd" / "sfo-kalshi-paper-scan.service.in")

    # Overlap guard: a slow scan must not be double-run by the 5-minute timer.
    assert "SFO_PAPER_SCAN_LOCK" in runner
    assert "flock -n" in runner
    assert runner.count("flock -n") == 1
    assert runner.count("    portfolio-scan") == 1
    assert "tail-basket" not in runner
    assert " arbitrage" not in runner
    assert " analyze" not in runner
    assert "SFO_PORTFOLIO_MAX_ARB_SPEND=12" in example_env
    for flag in (
        "PAPER_PLACE_LIVE",
        "PAPER_PLACE_RESEARCH_TARGET",
        "PAPER_PLACE_RESEARCH_MOTION",
    ):
        assert f'{flag}="${{{flag}:-0}}"' in runner
        assert f"{flag}=0" in example_env
    assert "SFO_PAPER_PLACE_ORDERS" not in runner
    assert "SFO_PAPER_PLACE_ORDERS" not in example_env
    assert "UnsetEnvironment=SFO_PAPER_PLACE_ORDERS" in service


def test_pull_paper_db_script_exists_for_offline_rescore():
    # The readiness rescore needs the live journal locally; sync_to_box.sh
    # only pushes OUT and excludes the DB, so a dedicated inbound pull must exist.
    puller = _read(AWS_DIR / "pull_paper_db.sh")
    assert "paper_trading.db" in puller
    assert "rsync" in puller
    assert "backtest-rescore" in puller  # documents the next step


def test_pull_paper_db_prefers_ec2_env_with_legacy_variable_fallback():
    puller = _read(AWS_DIR / "pull_paper_db.sh")

    assert ".local/ec2.env" in puller
    assert 'HOST_IP="${EC2_IP:-${LIGHTSAIL_IP:-}}"' in puller
    assert 'HOST_KEY="${EC2_KEY:-${LIGHTSAIL_KEY:-}}"' in puller


def test_box_sync_prefers_ec2_env_with_legacy_variable_fallback():
    syncer = _read(AWS_DIR / "sync_to_box.sh")

    assert ".local/ec2.env" in syncer
    assert 'HOST_IP="${EC2_IP:-${LIGHTSAIL_IP:-}}"' in syncer
    assert 'HOST_KEY="${EC2_KEY:-${LIGHTSAIL_KEY:-}}"' in syncer

    compatibility_wrapper = _read(AWS_DIR / "sync_to_lightsail.sh")
    assert "DEPRECATED" in compatibility_wrapper
    assert 'exec "$SCRIPT_DIR/sync_to_box.sh" "$@"' in compatibility_wrapper


def test_forecaster_syncs_share_runtime_exclude_manifest():
    full_sync = _read(AWS_DIR / "sync_to_box.sh")
    source_sync = _read(AWS_DIR / "sync_forecaster_source.sh")
    excludes = _read(AWS_DIR / "forecaster-runtime.rsync-filter")

    assert 'FORECASTER_EXCLUDES="$SCRIPT_DIR/forecaster-runtime.rsync-filter"' in full_sync
    assert 'FORECASTER_EXCLUDES="$SCRIPT_DIR/forecaster-runtime.rsync-filter"' in source_sync
    assert '--exclude-from="$FORECASTER_EXCLUDES"' in full_sync
    assert '--exclude-from="$FORECASTER_EXCLUDES"' in source_sync

    for artifact in (
        "STALE_FORECAST",
        "models/",
        "weather.db",
        "google_weather_cache.json",
        "trading_signal.json",
        "strategy_research.json",
        "cities_data.json",
        "publication_manifest.json",
    ):
        assert artifact in excludes


def test_full_box_sync_does_not_copy_local_runtime_state():
    syncer = _read(AWS_DIR / "sync_to_box.sh")

    assert "--exclude-from=\"$FORECASTER_EXCLUDES\"" in syncer
    assert "--exclude 'data'" in syncer


def test_tracked_forecaster_inputs_are_copied_to_the_box():
    full_sync = _read(AWS_DIR / "sync_to_box.sh")
    source_sync = _read(AWS_DIR / "sync_forecaster_source.sh")

    for artifact in (
        "forecast_data.json",
        "weather_story_data.json",
    ):
        assert f'--exclude "{artifact}"' not in full_sync
        assert f'--exclude "{artifact}"' not in source_sync


def test_retired_forecaster_refresh_gate_is_absent():
    needle = "SFO_ENABLE_" + "LIGHTSAIL_FORECASTER_REFRESH"
    result = subprocess.run(
        ["git", "grep", "-n", needle],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1, result.stdout


# ---------------------------------------------------------------------------
# T8-1 (Task 6 review, HIGH): a naive flip of the existing 38x/day
# sfo-forecaster-refresh unit to `--cities all` would spend 61 events/cycle
# and exhaust the 260/day hard cap by the 5th cycle, starving SFO for the
# rest of the day. Resolution: SFO stays on the frequent unit (see the
# sfo_refresh_unit tests above); the 14 non-SFO cities get their own
# once-daily unit here.
# ---------------------------------------------------------------------------


def _non_sfo_slugs() -> list[str]:
    return [city.slug for city in CITIES if city.slug != DEFAULT_CITY_SLUG]


def test_google_nonsfo_refresh_unit_covers_every_configured_non_sfo_city_once_daily():
    service = _read(AWS_DIR / "systemd" / "weatheredge-google-nonsfo-refresh.service.in")
    timer = _read(AWS_DIR / "systemd" / "weatheredge-google-nonsfo-refresh.timer")

    match = re.search(r"google_weather_cache\.py --cities (\S+)", service)
    assert match is not None, service
    configured = match.group(1).split(",")

    # Drift guard: this list is a static ExecStart argument (systemd units
    # cannot import cities.py), so if a city is ever added to or removed
    # from CITIES this test fails until the unit is updated to match.
    assert configured == _non_sfo_slugs()
    assert "sfo" not in configured
    assert len(configured) == 14

    assert "OnCalendar=" in timer
    # Exactly one calendar fire per day -- not the 38x/day SFO cadence.
    assert timer.count("OnCalendar=") == 1
    assert "Unit=weatheredge-google-nonsfo-refresh.service" in timer


def test_google_nonsfo_refresh_budget_arithmetic_matches_the_documented_schedule():
    service = _read(AWS_DIR / "systemd" / "weatheredge-google-nonsfo-refresh.service.in")
    sfo_service = _read(AWS_DIR / "systemd" / "sfo-forecaster-refresh.service.in")

    # The documented arithmetic (spec section 7.4) must be spelled out where
    # an operator reading the unit will see it: SFO 38x/day x 5 events = 190,
    # non-SFO 14 cities x 1x/day x 4 events = 56, total 246/day (7,626/31-day
    # month) -- comfortably under the 260/day hard cap and 7,800/month soft
    # ceiling.
    assert "190" in sfo_service
    assert "246" in service
    assert "56" in service
    assert "7,626" in service or "7626" in service


def test_google_nonsfo_refresh_unit_is_installed_alongside_sfo_refresh():
    installer = _read(AWS_DIR / "install_systemd.sh")
    notimers = _read(AWS_DIR / "install_systemd_notimers.sh")

    for script in (installer, notimers):
        assert "weatheredge-google-nonsfo-refresh.service.in" in script
        assert "weatheredge-google-nonsfo-refresh.timer" in script

    assert "weatheredge-google-nonsfo-refresh.timer" in installer[installer.index("systemctl enable --now"):]


# ---------------------------------------------------------------------------
# Item 1 (Task 8 plan step 2): /run/weatheredge creation, ownership, and
# cleanup via systemd tmpfiles.
# ---------------------------------------------------------------------------


def test_weatheredge_runtime_tmpfiles_entry_is_application_owned_and_protected():
    tmpfiles = _read(AWS_DIR / "systemd" / "weatheredge-tmpfiles.conf")
    lines = [line for line in tmpfiles.splitlines() if line.strip() and not line.startswith("#")]
    assert len(lines) == 1
    entry_type, path_field, mode, user, group, age = lines[0].split()
    assert entry_type == "d"
    assert path_field == "/run/weatheredge"
    # Not group- or world-writable (google_weather_store.assert_runtime_path's
    # documented requirement): the low two mode digits' write bits must be 0.
    assert mode[-2] in "0145"  # group: no write bit (0,1,4,5 have bit2 unset)
    assert mode[-1] in "0145"  # other: no write bit
    assert user == "__APP_USER__"
    assert group == "__APP_GROUP__"


def test_weatheredge_runtime_tmpfiles_entry_is_installed():
    installer = _read(AWS_DIR / "install_systemd.sh")
    notimers = _read(AWS_DIR / "install_systemd_notimers.sh")

    for script in (installer, notimers):
        assert "weatheredge-tmpfiles.conf" in script
        assert "/etc/tmpfiles.d/weatheredge.conf" in script
        assert "systemd-tmpfiles --create" in script


def test_weatheredge_tmpfiles_reset_behavior_is_documented_where_operators_will_see_it():
    """tmpfs is recreated empty on every reboot, so /run/weatheredge and its
    generation watermarks reset along with it -- expected, not a bug, but an
    operator reading the purge unit or the tmpfiles entry needs to see this
    stated plainly.
    """
    tmpfiles = _read(AWS_DIR / "systemd" / "weatheredge-tmpfiles.conf")
    purge_service = _read(AWS_DIR / "systemd" / "weatheredge-google-runtime-purge.service.in")

    assert "reboot" in tmpfiles.lower() or "reboot" in purge_service.lower()
    assert "watermark" in tmpfiles.lower() or "watermark" in purge_service.lower()


# ---------------------------------------------------------------------------
# Plan Task 8 step 2: the startup purge/expiry service.
# ---------------------------------------------------------------------------


def test_google_runtime_purge_unit_is_installed_and_scheduled():
    service = _read(AWS_DIR / "systemd" / "weatheredge-google-runtime-purge.service.in")
    timer = _read(AWS_DIR / "systemd" / "weatheredge-google-runtime-purge.timer")
    installer = _read(AWS_DIR / "install_systemd.sh")
    notimers = _read(AWS_DIR / "install_systemd_notimers.sh")

    assert "google_runtime_purge.py" in service
    assert "Unit=weatheredge-google-runtime-purge.service" in timer
    assert "OnCalendar=" in timer
    for script in (installer, notimers):
        assert "weatheredge-google-runtime-purge.service.in" in script
        assert "weatheredge-google-runtime-purge.timer" in script
    assert "weatheredge-google-runtime-purge.timer" in installer[installer.index("systemctl enable --now"):]


def test_google_runtime_purge_service_does_not_sync_or_back_up_the_runtime_db():
    """Plan Task 8 step 2: 'the runtime DB stays under /run with no
    backup/sync unit'.
    """
    service = _read(AWS_DIR / "systemd" / "weatheredge-google-runtime-purge.service.in")

    assert "backup" not in service.lower()
    assert "sync" not in service.lower()
    assert "s3" not in service.lower()


# ---------------------------------------------------------------------------
# Hard constraint: preserve the authoritative backup/restore gate. New
# timers must be known to the quiesce/capture/restore contract, or a full
# quiesce+restore cycle around a backup would silently leave them disabled.
# ---------------------------------------------------------------------------


def test_disable_systemd_timers_knows_about_every_installed_timer():
    disable_script = _read(AWS_DIR / "disable_systemd_timers.sh")
    installed_timers = sorted(path.name for path in (AWS_DIR / "systemd").glob("*.timer"))

    for timer in installed_timers:
        assert timer in disable_script, timer
