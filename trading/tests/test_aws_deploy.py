from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cities import CITIES, DEFAULT_CITY_SLUG
from sfo_kalshi_quant.account import LIVE_STABILITY_ACCOUNT_ID
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.live_execution import LiveExecutionPolicy
from sfo_kalshi_quant.models import TradeDecision


ROOT = Path(__file__).resolve().parents[2]
AWS_DIR = ROOT / "trading" / "deploy" / "aws"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _cutover_test_decision() -> TradeDecision:
    return TradeDecision(
        ticker="KXHIGHTSFO-26JUL27-B70.5",
        label="70° to 71°",
        action="BUY_NO",
        approved=True,
        probability=0.9,
        probability_lcb=0.85,
        yes_bid=0.2,
        yes_ask=0.21,
        spread=0.01,
        fee_per_contract=0.0,
        cost_per_contract=0.8,
        edge=0.1,
        edge_lcb=0.05,
        kelly_fraction=0.01,
        recommended_contracts=4.0,
        expected_profit=0.4,
        reasons=[],
        side="NO",
        entry_bid=0.79,
        entry_ask=0.8,
        entry_bid_size=100.0,
        entry_ask_size=100.0,
        trade_quality_score=80.0,
    )


def _explicit_utc_timer_start_seconds(timer: str) -> int:
    match = re.search(
        r"^OnCalendar=\*-\*-\* (\d{2}):(\d{2}):(\d{2}) UTC$",
        timer,
        re.MULTILINE,
    )
    assert match is not None, "maintenance timer must declare an explicit UTC time"
    hours, minutes, seconds = (int(value) for value in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _systemd_seconds(unit: str, setting: str) -> int:
    match = re.search(rf"^{re.escape(setting)}=(\d+)$", unit, re.MULTILINE)
    assert match is not None, f"{setting} must be an integer number of seconds"
    return int(match.group(1))


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


def test_installers_migrate_only_obsolete_publication_threshold_defaults():
    for name in ("install_systemd.sh", "install_systemd_notimers.sh"):
        installer = _read(AWS_DIR / name)
        assert 'grep -qx "SFO_PUBLICATION_MAX_OPERATIONAL_AGE_MINUTES=15"' in installer
        assert (
            "SFO_PUBLICATION_MAX_OPERATIONAL_AGE_MINUTES=15$/"
            "SFO_PUBLICATION_MAX_OPERATIONAL_AGE_MINUTES=10/"
        ) in installer
        assert 'grep -qx "SFO_PUBLICATION_MAX_PUBLIC_OPERATIONAL_AGE_MINUTES=10"' in installer
        assert (
            "SFO_PUBLICATION_MAX_PUBLIC_OPERATIONAL_AGE_MINUTES=10$/"
            "SFO_PUBLICATION_MAX_PUBLIC_OPERATIONAL_AGE_MINUTES=20/"
        ) in installer
        # The Pages lock wait must NOT be migrated upward. Holding that lock
        # means another publisher is already delivering, so this cycle defers
        # instead of queueing; a longer wait would eat the whole
        # TimeoutStartSec=900 service deadline before any work began.
        assert "SFO_PAGES_LOCK_WAIT_SECONDS=900" not in installer
        assert 'grep -qx "SFO_SCHEDULER_PROPAGATION_TIMEOUT_SECONDS=180"' in installer
        assert (
            "SFO_SCHEDULER_PROPAGATION_TIMEOUT_SECONDS=180$/"
            "SFO_SCHEDULER_PROPAGATION_TIMEOUT_SECONDS=420/"
        ) in installer
        assert "cp " not in installer[installer.index("# Migrate only"):installer.index("render_unit()")]


def test_env_migration_replaces_only_the_exact_legacy_live_risk_defaults(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "weatheredge.env"
    env_path.write_text(
        "SFO_LIVE_TRADING_ENABLED=0\n"
        "SFO_LIVE_TRADING_DRY_RUN=1\n"
        "SFO_LIVE_PILOT_MAX_LOSS=50\n"
        "SFO_LIVE_DAILY_LOSS=20\n"
        "SFO_LIVE_PER_TRADE_RISK=10\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(AWS_DIR / "migrate_weatheredge_env.py"),
            str(env_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "migrated legacy live risk defaults"
    migrated = env_path.read_text(encoding="utf-8")
    assert "SFO_LIVE_RISK_CAPITAL=1000" in migrated
    assert "SFO_LIVE_PILOT_MAX_LOSS_PCT=0.05" in migrated
    assert "SFO_LIVE_DAILY_LOSS_PCT=0.02" in migrated
    assert "SFO_LIVE_PER_TRADE_RISK_PCT=0.01" in migrated
    assert "SFO_LIVE_PILOT_MAX_LOSS=50" not in migrated
    assert "SFO_LIVE_DAILY_LOSS=20" not in migrated
    assert "SFO_LIVE_PER_TRADE_RISK=10" not in migrated
    assert "SFO_LIVE_TRADING_ENABLED=0" in migrated
    assert "SFO_LIVE_TRADING_DRY_RUN=1" in migrated


def test_env_migration_preserves_custom_operator_live_risk_caps(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "weatheredge.env"
    original = (
        "SFO_LIVE_TRADING_ENABLED=0\n"
        "SFO_LIVE_TRADING_DRY_RUN=1\n"
        "SFO_LIVE_PILOT_MAX_LOSS=75\n"
        "SFO_LIVE_DAILY_LOSS=30\n"
        "SFO_LIVE_PER_TRADE_RISK=12\n"
    )
    env_path.write_text(original, encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(AWS_DIR / "migrate_weatheredge_env.py"),
            str(env_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "live risk defaults unchanged"
    migrated = env_path.read_text(encoding="utf-8")
    assert migrated.startswith(original)
    assert "PAPER_SAME_DAY_MODEL_HEARTBEAT_ENABLED=true" in migrated
    assert "PAPER_RESEARCH_TAKE_PROFIT_MARGIN=0.05" in migrated


def test_env_migration_installs_audited_paper_defaults_without_overriding_custom_values(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "weatheredge.env"
    env_path.write_text(
        "PAPER_SAME_DAY_MODEL_HEARTBEAT_ENABLED=false\n",
        encoding="utf-8",
    )

    subprocess.run(
        [sys.executable, str(AWS_DIR / "migrate_weatheredge_env.py"), str(env_path)],
        check=True,
        capture_output=True,
        text=True,
    )

    migrated = env_path.read_text(encoding="utf-8")
    assert "PAPER_SAME_DAY_MODEL_HEARTBEAT_ENABLED=true" in migrated
    assert "PAPER_RESEARCH_TAKE_PROFIT_MARGIN=0.05" in migrated

    custom = (
        "PAPER_SAME_DAY_MODEL_HEARTBEAT_ENABLED=operator-managed\n"
        "PAPER_RESEARCH_TAKE_PROFIT_MARGIN=0.08\n"
    )
    env_path.write_text(custom, encoding="utf-8")
    subprocess.run(
        [sys.executable, str(AWS_DIR / "migrate_weatheredge_env.py"), str(env_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert env_path.read_text(encoding="utf-8") == custom


@pytest.mark.parametrize(
    "original",
    (
        (
            " SFO_LIVE_PILOT_MAX_LOSS=50\n"
            "SFO_LIVE_DAILY_LOSS=20\n"
            "SFO_LIVE_PER_TRADE_RISK=10\n"
        ),
        (
            "SFO_LIVE_PILOT_MAX_LOSS=50\n"
            "SFO_LIVE_DAILY_LOSS=20\n"
            "SFO_LIVE_PER_TRADE_RISK=10\n"
            "SFO_LIVE_PILOT_MAX_LOSS=75\n"
        ),
        (
            "SFO_LIVE_PILOT_MAX_LOSS=75\n"
            "SFO_LIVE_PILOT_MAX_LOSS=50\n"
            "SFO_LIVE_DAILY_LOSS=20\n"
            "SFO_LIVE_PER_TRADE_RISK=10\n"
        ),
    ),
)
def test_env_migration_preserves_ambiguous_legacy_assignments(
    tmp_path: Path,
    original: str,
) -> None:
    env_path = tmp_path / "weatheredge.env"
    env_path.write_text(original, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(AWS_DIR / "migrate_weatheredge_env.py"), str(env_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "live risk defaults unchanged"
    migrated = env_path.read_text(encoding="utf-8")
    assert migrated.startswith(original)
    assert "PAPER_SAME_DAY_MODEL_HEARTBEAT_ENABLED=true" in migrated
    assert "PAPER_RESEARCH_TAKE_PROFIT_MARGIN=0.05" in migrated


def test_installers_run_the_guarded_runtime_env_migration() -> None:
    installed_helper = "/usr/local/libexec/weatheredge/migrate_weatheredge_env.py"
    invocation = f'sudo /usr/bin/python3 -I "{installed_helper}" "$ENV_FILE"'
    for name in ("install_systemd.sh", "install_systemd_notimers.sh"):
        installer = _read(AWS_DIR / name)
        assert (
            'sudo install -m 755 "$SCRIPT_DIR/migrate_weatheredge_env.py" '
            f'"{installed_helper}"'
        ) in installer
        assert invocation in installer
        assert installer.index(invocation) < installer.index("render_unit()")
        assert 'sudo "$TRADING_DIR/.venv/bin/python"' not in installer


def test_live_risk_defaults_stay_aligned_across_runtime_example_and_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / "weatheredge.env"
    env_path.write_text(
        "SFO_LIVE_PILOT_MAX_LOSS=50\n"
        "SFO_LIVE_DAILY_LOSS=20\n"
        "SFO_LIVE_PER_TRADE_RISK=10\n",
        encoding="utf-8",
    )
    subprocess.run(
        [sys.executable, str(AWS_DIR / "migrate_weatheredge_env.py"), str(env_path)],
        check=True,
        capture_output=True,
        text=True,
    )

    relative_keys = (
        "SFO_LIVE_RISK_CAPITAL",
        "SFO_LIVE_PILOT_MAX_LOSS_PCT",
        "SFO_LIVE_DAILY_LOSS_PCT",
        "SFO_LIVE_PER_TRADE_RISK_PCT",
    )

    def assignments(text: str) -> dict[str, str]:
        return {
            key: value
            for line in text.splitlines()
            if line and not line.startswith("#") and "=" in line
            for key, value in [line.split("=", 1)]
        }

    migrated = assignments(env_path.read_text(encoding="utf-8"))
    example = assignments(_read(AWS_DIR / "sfo-weather.env.example"))
    assert {key: migrated[key] for key in relative_keys} == {
        key: example[key] for key in relative_keys
    }

    for name in (
        "SFO_LIVE_PILOT_MAX_LOSS",
        "SFO_LIVE_DAILY_LOSS",
        "SFO_LIVE_PER_TRADE_RISK",
    ):
        monkeypatch.delenv(name, raising=False)
    for key in relative_keys:
        monkeypatch.setenv(key, migrated[key])

    policy = LiveExecutionPolicy.from_env()
    assert policy.pilot_max_loss == pytest.approx(50.0)
    assert policy.daily_loss == pytest.approx(20.0)
    assert policy.per_trade_risk == pytest.approx(10.0)


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
    assert "sync_forecaster_source.sh" not in text
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


def test_google_refresh_units_do_not_hide_emos_baseline_failure():
    for service_name in (
        "sfo-forecaster-refresh.service.in",
        "weatheredge-google-nonsfo-refresh.service.in",
    ):
        text = _read(AWS_DIR / "systemd" / service_name)
        google_exec = next(
            line
            for line in text.splitlines()
            if line.startswith("ExecStart=") and "google_weather_cache.py" in line
        )

        assert not google_exec.startswith("ExecStart=-")


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
    assert "sync_forecaster_source.sh" not in service
    assert "run_publication_cycle.sh operational" in service
    assert "google_weather_cache.py --refresh" not in service
    assert "OnActiveSec=" not in timer
    assert "OnBootSec=" not in timer
    assert "OnUnitActiveSec=" not in timer
    assert "OnCalendar=*-*-* *:02,12,22,32,42,52" in timer
    assert "Unit=sfo-operational-publish.service" in timer
    assert "Nice=10" in service
    assert "CPUWeight=50" in service


def test_web_app_deploy_triggers_fast_operational_publication():
    deployer = _read(AWS_DIR / "deploy_web_app.sh")

    assert "systemctl start sfo-operational-publish.service" in deployer
    assert "systemctl start sfo-strategy-lab-refresh.service" not in deployer


def test_strategy_lab_refresh_uses_bounded_fast_publication():
    installer = _read(AWS_DIR / "install_systemd.sh")
    service = _read(AWS_DIR / "systemd" / "sfo-strategy-lab-refresh.service.in")
    timer = _read(AWS_DIR / "systemd" / "sfo-strategy-lab-refresh.timer")

    assert "sfo-strategy-lab-refresh.service.in" in installer
    assert "sfo-strategy-lab-refresh.timer" in installer
    assert "sfo-strategy-lab-refresh.timer" in installer
    assert "run_publication_cycle.sh strategy" in service
    assert "build_public_trading_signal.sh" not in service
    assert "google_weather_cache.py --refresh" not in service
    assert (
        "OnCalendar=*-*-* *:00,05,10,15,20,25,30,35,40,45,50,55"
        in timer
    )
    assert "Persistent=true" in timer
    assert "OnActiveSec=" not in timer
    assert "OnBootSec=" not in timer
    assert "OnUnitActiveSec=" not in timer
    assert "OnUnitInactiveSec=" not in timer
    assert "Unit=sfo-strategy-lab-refresh.service" in timer
    assert "TimeoutStartSec=120" in service
    assert "Nice=10" in service
    assert "CPUWeight=50" in service
    runner = _read(AWS_DIR / "run_publication_cycle.sh")
    assert "export SFO_STRATEGY_FAST_PUBLICATION=1" in runner
    assert "Environment=SFO_STRATEGY_FAST_PUBLICATION=1" not in service
    assert "sync_forecaster_source.sh" not in service
    assert "SFO_STRATEGY_FAST_PUBLICATION=1" in _read(
        AWS_DIR / "sfo-weather.env.example"
    )


def test_publication_lock_waits_leave_service_deadline_headroom():
    runner = _read(AWS_DIR / "run_publication_cycle.sh")
    publisher = _read(AWS_DIR / "publish_forecaster_pages.sh")
    strategy_service = _read(
        AWS_DIR / "systemd" / "sfo-strategy-lab-refresh.service.in"
    )
    operational_service = _read(
        AWS_DIR / "systemd" / "sfo-operational-publish.service.in"
    )

    strategy_wait = int(
        re.search(
            r"SFO_STRATEGY_ARTIFACT_LOCK_WAIT_SECONDS:-([0-9]+)",
            runner,
        ).group(1)
    )
    operational_wait = int(
        re.search(
            r"SFO_OPERATIONAL_ARTIFACT_LOCK_WAIT_SECONDS:-([0-9]+)",
            runner,
        ).group(1)
    )
    pages_wait = int(
        re.search(r"SFO_PAGES_LOCK_WAIT_SECONDS:-([0-9]+)", publisher).group(1)
    )
    strategy_deadline = _systemd_seconds(strategy_service, "TimeoutStartSec")
    operational_deadline = _systemd_seconds(
        operational_service,
        "TimeoutStartSec",
    )

    assert strategy_wait <= 30
    assert strategy_wait < strategy_deadline / 2
    assert operational_wait + pages_wait <= 120
    assert operational_wait + pages_wait < operational_deadline / 2
    assert 'flock -w "$PAGES_LOCK_WAIT_SECONDS" 9' in publisher
    assert "SFO_ARTIFACT_LOCK_WAIT_SECONDS" not in runner
    assert "SFO_ARTIFACT_LOCK_WAIT_SECONDS" not in publisher


def test_scheduler_health_watchdog_is_bounded_and_wired_everywhere():
    script = _read(AWS_DIR / "check_scheduler_health.sh")
    service = _read(AWS_DIR / "systemd" / "sfo-scheduler-health.service.in")
    timer = _read(AWS_DIR / "systemd" / "sfo-scheduler-health.timer")
    installer = _read(AWS_DIR / "install_systemd.sh")
    notimers = _read(AWS_DIR / "install_systemd_notimers.sh")
    quiesce = _read(AWS_DIR / "disable_systemd_timers.sh")
    integrity = _read(AWS_DIR / "verify_systemd_unit_integrity.sh")

    assert "OnCalendar=*-*-* *:03,08,13,18,23,28,33,38,43,48,53,58" in timer
    assert "Persistent=true" in timer
    assert "Unit=sfo-scheduler-health.service" in timer
    assert "OnFailure=sfo-alert@%n.service" in service
    assert "Environment=SFO_SCHEDULER_APP_USER=__APP_USER__" in service
    assert (
        "ExecStart=/usr/local/libexec/weatheredge/check_scheduler_health.sh"
        in service
    )
    assert "RuntimeDirectory=weatheredge-scheduler-health" in service
    assert "TimeoutStartSec=1500" in service
    assert "\nUser=" not in service

    for install_script in (installer, notimers):
        assert "check_scheduler_health.sh" in install_script
        assert "sfo-scheduler-health.service.in" in install_script
        assert "sfo-scheduler-health.timer" in install_script
    assert "sfo-scheduler-health.timer" in installer[installer.index("systemctl enable --now") :]
    assert (
        "sfo-scheduler-health.timer sfo-scheduler-health.service"
        in quiesce
    )
    assert "sfo-scheduler-health.service" in integrity
    assert "sfo-scheduler-health.timer" in integrity

    canonical_block = script[
        script.index("CANONICAL_TIMERS=(") : script.index(
            "if [[ -e \"$DEPLOY_MAINTENANCE_MARKER\""
        )
    ]
    assert canonical_block.count(".timer\"") == 13
    assert "sfo-scheduler-health.timer" not in canonical_block
    repair_targets = set(
        re.findall(r'SYSTEMCTL\[@\]}" start ([a-z0-9@.-]+)', script)
    )
    assert repair_targets == {
        "sfo-strategy-lab-refresh.service",
        "sfo-operational-publish.service",
    }
    assert "PAPER_PLACE_" not in script
    assert "SFO_LIVE_TRADING" not in script


def test_deploy_maintenance_marker_holds_scheduler_repair_until_restore():
    deployer = _read(AWS_DIR / "sync_to_box.sh")

    capture_idx = deployer.index("bash -s capture")
    marker_create_idx = deployer.index(
        "sudo install -o root -g root -m 600 /dev/null "
        "'$DEPLOY_MAINTENANCE_MARKER'"
    )
    quiesce_idx = deployer.index('bash -s quiesce < "$QUIESCE_HELPER"')
    scheduler_classify_idx = deployer.index(
        'if [[ "$timer" == "sfo-scheduler-health.timer" ]]'
    )
    scheduler_restore_idx = deployer.index(
        "bash -s restore sfo-scheduler-health.timer"
    )
    marker_remove_idx = deployer.rindex(
        "sudo rm -f -- '$DEPLOY_MAINTENANCE_MARKER'"
    )
    scheduler_check_idx = deployer.rindex(
        "sudo systemctl start sfo-scheduler-health.service"
    )

    assert capture_idx < marker_create_idx < quiesce_idx
    assert quiesce_idx < scheduler_classify_idx
    assert scheduler_classify_idx < scheduler_restore_idx
    assert scheduler_restore_idx < marker_remove_idx < scheduler_check_idx
    assert "SCHEDULER_WATCHDOG_ENABLED=0" in deployer
    assert "bash -s probe sfo-scheduler-health.timer" in deployer
    assert "SCHEDULER_WATCHDOG_WAS_ABSENT=1" in deployer
    assert 'ENABLED_TIMERS+=("sfo-scheduler-health.timer")' in deployer


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


def test_operational_publication_serializes_builder_and_snapshot_under_shared_lock():
    runner = _read(AWS_DIR / "run_publication_cycle.sh")
    example_env = _read(AWS_DIR / "sfo-weather.env.example")

    assert 'SFO_ARTIFACT_GENERATION_LOCK:-/opt/weatheredge/.locks/artifact-generation.lock' in runner
    assert "flock" in runner
    assert "SFO_ARTIFACT_LOCK_HELD=1" in runner
    assert "build_public_trading_signal.sh" in runner
    assert "build_strategy_research.sh" in runner
    assert "publish_forecaster_pages.sh" in runner
    assert "SFO_STRATEGY_BUILD_STAGING=1" in runner
    assert runner.index("build_public_trading_signal.sh") < runner.index("publish_forecaster_pages.sh")
    assert "SFO_ARTIFACT_GENERATION_LOCK=/opt/weatheredge/.locks/artifact-generation.lock" in example_env


def test_publication_cycle_releases_generation_lock_during_pages_delivery_wait():
    runner = _read(AWS_DIR / "run_publication_cycle.sh")
    publisher = _read(AWS_DIR / "publish_forecaster_pages.sh")

    operational_cycle = runner[runner.index("esac") :]
    held_idx = operational_cycle.index("export SFO_ARTIFACT_LOCK_HELD=1")
    fd_idx = operational_cycle.index("export SFO_ARTIFACT_LOCK_FD=7")
    build_idx = operational_cycle.index('/bin/bash "$BUILDER"')
    publish_idx = operational_cycle.index('publish_forecaster_pages.sh')
    assert held_idx < fd_idx < build_idx < publish_idx
    assert "flock -u 7" not in operational_cycle

    inherited_unlock_idx = publisher.index("flock -u 7")
    inherited_close_idx = publisher.index("exec 7>&-")
    git_init_idx = publisher.index("git init")
    fetch_idx = publisher.index("git fetch")
    # The gate is invoked in a conditional now: a deferral must return to the
    # caller as a clean exit 0, not abort the publisher under `set -e`.
    delivery_gate_idx = publisher.index("if ! prepare_pages_branch; then")
    reacquire_idx = publisher.index('exec 8>"$ARTIFACT_LOCK"')
    snapshot_copy_idx = publisher.index('cp "$source_path"')
    final_unlock_idx = publisher.rindex("flock -u 8")
    push_idx = publisher.index("git push")
    assert (
        inherited_unlock_idx
        < inherited_close_idx
        < git_init_idx
        < fetch_idx
        < delivery_gate_idx
        < reacquire_idx
        < snapshot_copy_idx
        < final_unlock_idx
        < push_idx
    )


def test_strategy_cycle_rebuilds_manifest_but_never_competes_with_operational_publisher():
    runner = _read(AWS_DIR / "run_publication_cycle.sh")

    research_idx = runner.index("build_strategy_research.sh")
    staging_idx = runner.index("SFO_STRATEGY_BUILD_STAGING=1")
    strategy_lock_idx = runner.index('exec 7>"$ARTIFACT_LOCK"')
    manifest_idx = runner.index("sfo_kalshi_quant.publication build")
    deferred_idx = runner.index("publication deferred to the operational cycle")
    assert research_idx < staging_idx < strategy_lock_idx < manifest_idx < deferred_idx
    assert runner.index('mv -f -- "$strategy_promote_tmp"') < manifest_idx
    assert "strategy_analysis_cache.json" not in runner
    assert "strategy_analysis_promote_tmp" not in runner
    assert "SFO_STRATEGY_PUBLISH" not in runner


def test_slow_strategy_compute_does_not_block_operational_cycle(tmp_path: Path):
    aws_dir = tmp_path / "aws"
    aws_dir.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_flock = fake_bin / "flock"
    fake_flock.write_text(
        "#!/usr/bin/env python3\n"
        "import fcntl, sys, time\n"
        "args = sys.argv[1:]\n"
        "if args[0] == '-u':\n"
        "    fcntl.flock(int(args[1]), fcntl.LOCK_UN)\n"
        "    raise SystemExit(0)\n"
        "if args[0] == '-w':\n"
        "    deadline = time.monotonic() + float(args[1])\n"
        "    fd = int(args[2])\n"
        "    while True:\n"
        "        try:\n"
        "            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "            raise SystemExit(0)\n"
        "        except BlockingIOError:\n"
        "            if time.monotonic() >= deadline:\n"
        "                raise SystemExit(1)\n"
        "            time.sleep(0.02)\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    fake_flock.chmod(0o755)
    runner = aws_dir / "run_publication_cycle.sh"
    runner.write_text(_read(AWS_DIR / "run_publication_cycle.sh"), encoding="utf-8")
    runner.chmod(0o755)

    started = tmp_path / "strategy-started"
    release = tmp_path / "release-strategy"
    operational_done = tmp_path / "operational-done"
    strategy_builder = aws_dir / "build_strategy_research.sh"
    strategy_builder.write_text(
        "#!/bin/bash\n"
        "set -eu\n"
        'touch "$STRATEGY_STARTED"\n'
        'while [[ ! -e "$STRATEGY_RELEASE" ]]; do sleep 0.05; done\n'
        'printf "{}\\n" > \"$SFO_STRATEGY_RESEARCH_PATH\"\n',
        encoding="utf-8",
    )
    strategy_builder.chmod(0o755)
    operational_builder = aws_dir / "build_public_trading_signal.sh"
    operational_builder.write_text("#!/bin/bash\nset -eu\n", encoding="utf-8")
    operational_builder.chmod(0o755)
    publisher = aws_dir / "publish_forecaster_pages.sh"
    publisher.write_text(
        "#!/bin/bash\n"
        "set -eu\n"
        'touch "$OPERATIONAL_DONE"\n'
        'flock -u "$SFO_ARTIFACT_LOCK_FD"\n',
        encoding="utf-8",
    )
    publisher.chmod(0o755)
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/bash\n"
        "set -eu\n"
        "while (($#)); do\n"
        '  if [[ "$1" == "--output" ]]; then printf "{}\\n" > "$2"; exit 0; fi\n'
        "  shift\n"
        "done\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    forecaster = tmp_path / "forecaster"
    trading = tmp_path / "trading"
    forecaster.mkdir()
    trading.mkdir()
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SFO_FORECASTER_ROOT": str(forecaster),
        "SFO_TRADING_ROOT": str(trading),
        "SFO_TRADING_PYTHON": str(fake_python),
        "SFO_ARTIFACT_GENERATION_LOCK": str(tmp_path / "artifact.lock"),
        "SFO_STRATEGY_ARTIFACT_LOCK_WAIT_SECONDS": "2",
        "SFO_OPERATIONAL_ARTIFACT_LOCK_WAIT_SECONDS": "2",
        "STRATEGY_STARTED": str(started),
        "STRATEGY_RELEASE": str(release),
        "OPERATIONAL_DONE": str(operational_done),
    }
    strategy = subprocess.Popen(["bash", str(runner), "strategy"], env=env)
    try:
        deadline = time.monotonic() + 2
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert started.exists()

        operational = subprocess.run(
            ["bash", str(runner), "operational"],
            env=env,
            check=False,
            timeout=1,
        )
        assert operational.returncode == 0
        assert operational_done.exists()
    finally:
        release.touch()
        strategy.wait(timeout=3)
    assert strategy.returncode == 0


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
    prune_service = _read(
        AWS_DIR / "systemd" / "sfo-kalshi-paper-prune.service.in"
    )
    prune_timer = _read(AWS_DIR / "systemd" / "sfo-kalshi-paper-prune.timer")
    runner = _read(AWS_DIR / "run_dataset_backfill.sh")
    example_env = _read(AWS_DIR / "sfo-weather.env.example")

    assert "sfo-dataset-backfill.service.in" in installer
    assert "sfo-dataset-backfill.timer" in installer
    assert "sfo-dataset-backfill.timer" in installer
    assert "run_dataset_backfill.sh" in service
    assert "EnvironmentFile=__ENV_FILE__" in service
    dataset_start = _explicit_utc_timer_start_seconds(timer)
    prune_latest_finish = (
        _explicit_utc_timer_start_seconds(prune_timer)
        + _systemd_seconds(prune_timer, "RandomizedDelaySec")
        + _systemd_seconds(prune_timer, "AccuracySec")
        + _systemd_seconds(prune_service, "TimeoutStartSec")
    )
    assert _systemd_seconds(timer, "AccuracySec") == 1
    assert dataset_start - prune_latest_finish >= 300
    assert "Persistent=false" in timer
    assert "Persistent=true" not in timer
    assert "Unit=sfo-dataset-backfill.service" in timer

    # `lamp` is excluded: NOMADS no longer serves the product tree at all.
    assert 'SFO_DATASET_SOURCES="${SFO_DATASET_SOURCES:-iem-asos,open-meteo-previous-runs,open-meteo-historical-forecast,gfs-mos,nbm,hrrr,kalshi-history}"' in runner
    default_sources = "SFO_DATASET_SOURCES=iem-asos,open-meteo-previous-runs,open-meteo-historical-forecast,gfs-mos,nbm,hrrr,kalshi-history"
    assert default_sources in example_env
    assert (
        "SFO_DATASET_RESEARCH_PATH=/opt/weatheredge/forecaster/dataset_research.json"
        in example_env
    )
    assert "dataset-backfill" in runner
    assert "--source noaa-isd" not in runner
    assert 'SFO_DATASET_DB:-${SFO_KALSHI_DB:-$TRADING_DIR/data/paper_trading.db}' in runner
    assert "failed_sources=()" in runner
    assert "continuing" in runner
    # A single bad night from an upstream provider must not fail the unit; only
    # a source failing on consecutive runs does.
    assert 'FAILURE_THRESHOLD="${SFO_DATASET_SOURCE_FAILURE_THRESHOLD:-2}"' in runner
    assert "sustained_failures" in runner
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
    # The archive-then-prune chain runs long; it must outlive the 90 s default,
    # with room for a cold-cache catch-up night on the 14+ GB journal.
    assert "TimeoutStartSec=3600" in service
    assert "OnCalendar=*-*-* 08:20:00 UTC" in timer
    assert "Persistent=false" in timer
    assert "Persistent=true" not in timer
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
    wrapper = _read(AWS_DIR / "run_archive_then_prune.sh")

    assert "SFO_PRUNE_MODE=archive-only" in example_env
    assert 'PRUNE_MODE="${SFO_PRUNE_MODE:-archive-only}"' in wrapper
    assert '[[ "$PRUNE_MODE" == "quiesced-delete" ]]' in wrapper
    assert "SFO_PRUNE_FULL_DAYS=1" in example_env


def test_source_only_sync_is_disabled_to_preserve_cross_tree_provenance():
    syncer = _read(AWS_DIR / "sync_forecaster_source.sh")

    assert "is disabled" in syncer
    assert "sync_to_box.sh" in syncer
    assert "git fetch" not in syncer
    assert "rsync" not in syncer


def test_full_sync_preserves_stale_forecast_watchdog_marker():
    excludes = _read(AWS_DIR / "forecaster-runtime.rsync-filter")
    syncer = _read(AWS_DIR / "sync_to_box.sh")
    assert '--exclude-from="$FORECASTER_EXCLUDES"' in syncer
    assert "STALE_FORECAST" in excludes


def test_pages_publish_ships_spa_and_fresh_jsons():
    publisher = _read(AWS_DIR / "publish_forecaster_pages.sh")
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
    assert "strategy_analysis_cache.json" in excludes
    assert "strategy_research_evidence.private.json" in excludes
    assert "cities_data.json" in excludes
    assert "publication_manifest.json" in excludes


def test_forecaster_filter_preserves_build_provenance():
    """Runtime exclusions keep the full deploy from replacing its own stamp."""

    excludes = _read(AWS_DIR / "forecaster-runtime.rsync-filter")
    assert "build_info.json" in excludes


def test_pages_deploy_key_path_matches_ec2_setup_docs():
    example_env = _read(AWS_DIR / "sfo-weather.env.example")
    publisher = _read(AWS_DIR / "publish_forecaster_pages.sh")
    readme = _read(AWS_DIR / "README.md")

    expected = "sfo_weather_pages_deploy"
    assert expected in example_env
    assert expected in publisher
    assert expected in readme
    assert "weatheredge_pages_deploy" not in example_env + publisher + readme


def test_disabled_source_sync_documents_the_controlled_replacement():
    syncer = _read(AWS_DIR / "sync_forecaster_source.sh")
    readme = _read(AWS_DIR / "README.md")

    assert "both source trees" in syncer
    assert "disabled compatibility tombstone" in readme


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


def test_strategy_cycle_never_invokes_the_pages_publisher():
    runner = _read(AWS_DIR / "run_publication_cycle.sh")
    publisher = _read(AWS_DIR / "publish_forecaster_pages.sh")

    assert "--require-strategy" in publisher
    strategy_start = runner.index("  strategy)")
    strategy_block = runner[strategy_start : runner.index("  *)", strategy_start)]
    assert strategy_block.index("sfo_kalshi_quant.publication build") < strategy_block.index("exit 0")
    assert "publish_forecaster_pages.sh" not in strategy_block
    assert "SFO_REQUIRE_STRATEGY_ARTIFACT" not in runner


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
    assert "SFO_PUBLICATION_MAX_OPERATIONAL_AGE_MINUTES=10" in example_env
    assert "SFO_PUBLICATION_MAX_PUBLIC_OPERATIONAL_AGE_MINUTES=20" in example_env
    assert "SFO_PUBLICATION_MAX_STRATEGY_AGE_MINUTES=20" in example_env
    # Offline historical analysis fails closed inside Strategy Lab itself. The
    # operational watchdog must not promise a fast repair it cannot perform.
    assert "SFO_PUBLICATION_MAX_STRATEGY_ANALYSIS_AGE_MINUTES" not in example_env
    assert (
        "SFO_PUBLICATION_MANIFEST_URL="
        "https://jaxsonb04.github.io/weather_edge/publication_manifest.json"
    ) in example_env
    assert "shared sfo-alert@.service JSON" in watchdog
    assert "Slack/Discord" not in watchdog
    for documentation in (readme, deployment):
        assert "10 minutes" in documentation
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
        assert "wall-clock five-minute cadence" in normalized
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


def test_deploy_builds_decision_indexes_while_services_are_quiesced():
    deployer = _read(AWS_DIR / "sync_to_box.sh")

    quiesce_idx = deployer.index('bash -s quiesce < "$QUIESCE_HELPER"')
    index_idx = deployer.index("bash deploy/aws/create_decision_snapshot_index.sh")
    restore_idx = deployer.index("PRODUCER_TIMERS=()")
    publication_idx = deployer.index("bash deploy/aws/wait_for_publication_manifest.sh")
    analysis_idx = deployer.index(
        "bash deploy/aws/refresh_strategy_analysis_cache.sh"
    )
    assert quiesce_idx < index_idx < analysis_idx < restore_idx < publication_idx
    assert "WEATHEREDGE_BACKUP_SNAPSHOT=" in deployer
    assert "SFO_STRATEGY_ANALYSIS_DB_PATH=" in deployer


def test_deploy_initializes_account_cutover_before_any_producer_or_seed():
    deployer = _read(AWS_DIR / "sync_to_box.sh")
    validator = AWS_DIR / "validate_account_cutover.py"

    quiesce_idx = deployer.index('bash -s quiesce < "$QUIESCE_HELPER"')
    cutover_idx = deployer.index("deploy/aws/validate_account_cutover.py")
    producer_restore_idx = deployer.index("PRODUCER_TIMERS=()")
    seed_idx = deployer.index(
        "sudo systemctl start sfo-strategy-lab-refresh.service"
    )
    assert quiesce_idx < cutover_idx < producer_restore_idx < seed_idx
    assert validator.is_file()


def test_account_cutover_validator_imports_from_exact_deploy_cwd_without_pythonpath():
    validator = AWS_DIR / "validate_account_cutover.py"
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, str(validator), "--help"],
        cwd=ROOT / "trading",
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--db" in result.stdout


def test_account_cutover_validator_fails_closed_on_tampered_capital(tmp_path):
    db_path = tmp_path / "paper.db"
    store = PaperStore(db_path)
    validator = AWS_DIR / "validate_account_cutover.py"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "trading")}

    healthy = subprocess.run(
        [sys.executable, str(validator), "--db", str(db_path)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert healthy.returncode == 0, healthy.stderr

    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_accounts SET opening_cash=900 WHERE account_id=?",
            (LIVE_STABILITY_ACCOUNT_ID,),
        )
    tampered = subprocess.run(
        [sys.executable, str(validator), "--db", str(db_path)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert tampered.returncode != 0
    assert "active paper account capital is invalid" in tampered.stderr


def test_failed_account_cutover_validation_does_not_raise_high_water(tmp_path):
    db_path = tmp_path / "paper.db"
    store = PaperStore(db_path)
    validator = AWS_DIR / "validate_account_cutover.py"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "trading")}
    order_id = store.record_paper_order(
        "2026-07-27",
        _cutover_test_decision(),
        risk_profile="live",
    )
    assert order_id is not None
    with store.connect() as conn:
        before = conn.execute(
            "SELECT high_water_equity FROM paper_accounts WHERE account_id=?",
            (LIVE_STABILITY_ACCOUNT_ID,),
        ).fetchone()[0]
        conn.execute(
            "DELETE FROM paper_account_ledger "
            "WHERE order_id=? AND event_type='ENTRY_FILL'",
            (order_id,),
        )

    result = subprocess.run(
        [sys.executable, str(validator), "--db", str(db_path)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    with store.connect() as conn:
        after = conn.execute(
            "SELECT high_water_equity FROM paper_accounts WHERE account_id=?",
            (LIVE_STABILITY_ACCOUNT_ID,),
        ).fetchone()[0]

    assert result.returncode != 0
    assert "does not reconcile" in result.stderr
    assert after == before


def test_deploy_keeps_recurring_manifest_writers_stopped_until_seed_is_public():
    deployer = _read(AWS_DIR / "sync_to_box.sh")

    publisher_classify_idx = deployer.index(
        'elif [[ "$timer" == "sfo-operational-publish.timer" ]]'
    )
    strategy_classify_idx = deployer.index(
        'elif [[ "$timer" == "sfo-strategy-lab-refresh.timer" ]]'
    )
    seed_idx = deployer.index(
        "sudo systemctl start sfo-strategy-lab-refresh.service"
    )
    wait_idx = deployer.index(
        "bash deploy/aws/wait_for_publication_manifest.sh"
    )
    restore_idx = deployer.index(
        'if restore_initial_timers "$(( INITIAL_SEED_STATUS != 0 ))"; then'
    )

    assert publisher_classify_idx < seed_idx
    assert strategy_classify_idx < seed_idx
    assert seed_idx < wait_idx < restore_idx
    assert "PUBLISH_TIMER_ENABLED=0" in deployer
    assert "PUBLISH_TIMER_ENABLED=1" in deployer
    assert "STRATEGY_TIMER_ENABLED=0" in deployer
    assert "STRATEGY_TIMER_ENABLED=1" in deployer
    assert "INITIAL_HELD_TIMERS" in deployer
    assert "recover_deploy_runtime" in deployer
    assert "RUNTIME_RECOVERY_REQUIRED=1" in deployer
    assert "RUNTIME_RECOVERY_REQUIRED=0" in deployer


def test_deploy_verifies_canonical_systemd_units_before_restoring_timers():
    deployer = _read(AWS_DIR / "sync_to_box.sh")
    guard = _read(AWS_DIR / "verify_systemd_unit_integrity.sh")

    install_idx = deployer.index("bash deploy/aws/install_systemd_notimers.sh")
    verify_idx = deployer.index("bash deploy/aws/verify_systemd_unit_integrity.sh")
    restore_idx = deployer.index("PRODUCER_TIMERS=()")
    assert install_idx < verify_idx < restore_idx
    assert 'SYSTEMD_VERIFY_HELPER="$SCRIPT_DIR/verify_systemd_unit_integrity.sh"' in deployer
    assert 'if [[ ! -f "$SYSTEMD_VERIFY_HELPER" ]]' in deployer

    for definition in (AWS_DIR / "systemd").glob("*.service.in"):
        assert definition.name.removesuffix(".in") in guard
    for definition in (AWS_DIR / "systemd").glob("*.timer"):
        assert definition.name in guard
    for property_name in (
        "FragmentPath",
        "DropInPaths",
        "Transient",
        "NeedDaemonReload",
    ):
        assert property_name in guard
    assert "/etc/systemd/system" in guard
    assert "sfo-strategy-lab-refresh.service" in guard
    assert "TimeoutStartUSec" in guard
    assert "2min" in guard


def test_systemd_drift_guard_rejects_effective_unit_drift(
    tmp_path: Path,
):
    guard = AWS_DIR / "verify_systemd_unit_integrity.sh"
    fake = tmp_path / "systemctl"
    fake.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
[[ "$1" == "show" ]]
unit="${@: -1}"
fragment_unit="$unit"
if [[ "$unit" == "sfo-alert@weatheredge-integrity.service" ]]; then
  fragment_unit="sfo-alert@.service"
fi
for arg in "$@"; do
  case "$arg" in
    --property=FragmentPath)
      if [[ "${FAKE_FRAGMENT_UNIT:-}" == "$unit" ]]; then
        printf 'FragmentPath=/run/systemd/transient/%s\\n' "$unit"
      else
        printf 'FragmentPath=/etc/systemd/system/%s\\n' "$fragment_unit"
      fi
      ;;
    --property=DropInPaths)
      if [[ "${FAKE_DROP_IN_UNIT:-}" == "$unit" ]]; then
        printf 'DropInPaths=/run/systemd/system/%s.d/audit-timeout.conf\\n' "$unit"
      else
        printf 'DropInPaths=\\n'
      fi
      ;;
    --property=Transient)
      if [[ "${FAKE_TRANSIENT_UNIT:-}" == "$unit" ]]; then
        printf 'Transient=yes\\n'
      else
        printf 'Transient=no\\n'
      fi
      ;;
    --property=NeedDaemonReload)
      if [[ "${FAKE_RELOAD_UNIT:-}" == "$unit" ]]; then
        printf 'NeedDaemonReload=yes\\n'
      else
        printf 'NeedDaemonReload=no\\n'
      fi
      ;;
    --property=TimeoutStartUSec)
      if [[ "$unit" == "sfo-strategy-lab-refresh.service" ]]; then
        printf 'TimeoutStartUSec=%s\\n' "${FAKE_STRATEGY_TIMEOUT:-2min}"
      else
        printf 'TimeoutStartUSec=infinity\\n'
      fi
      ;;
  esac
done
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    base_env = {**os.environ, "SYSTEMCTL_BIN": str(fake)}

    valid = subprocess.run(
        ["bash", str(guard)],
        env=base_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert valid.returncode == 0, valid.stderr
    assert 'inspect_unit="sfo-alert@weatheredge-integrity.service"' in guard.read_text()

    drift_cases = (
        ("FAKE_FRAGMENT_UNIT", "unexpected FragmentPath"),
        ("FAKE_DROP_IN_UNIT", "unexpected DropInPaths"),
        ("FAKE_TRANSIENT_UNIT", "unexpected Transient"),
        ("FAKE_RELOAD_UNIT", "unexpected NeedDaemonReload"),
    )
    for env_name, expected_error in drift_cases:
        drift = subprocess.run(
            ["bash", str(guard)],
            env={
                **base_env,
                env_name: "sfo-strategy-lab-refresh.service",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert drift.returncode != 0
        assert expected_error in drift.stderr

    timeout = subprocess.run(
        ["bash", str(guard)],
        env={**base_env, "FAKE_STRATEGY_TIMEOUT": "30min"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert timeout.returncode != 0
    assert "TimeoutStartUSec" in timeout.stderr
    assert "expected 2min" in timeout.stderr

    unit_root = tmp_path / "systemd"
    instance_drop_in = (
        unit_root
        / "sfo-alert@sfo-strategy-lab-refresh.service.service.d"
    )
    instance_drop_in.mkdir(parents=True)
    (instance_drop_in / "runtime.conf").write_text(
        "[Service]\nTimeoutStartSec=30min\n",
        encoding="utf-8",
    )
    instance_drift = subprocess.run(
        ["bash", str(guard)],
        env={
            **base_env,
            "SYSTEMD_UNIT_SEARCH_ROOTS": str(unit_root),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert instance_drift.returncode != 0
    assert "unexpected alert-instance systemd override" in instance_drift.stderr


def test_quiesce_stops_transient_strategy_analysis_without_restoring_it():
    deployer = _read(AWS_DIR / "sync_to_box.sh")
    helper = _read(AWS_DIR / "disable_systemd_timers.sh")

    quiesce_idx = deployer.index('bash -s quiesce < "$QUIESCE_HELPER"')
    backup_idx = deployer.index('bash -s backup "$REMOTE_DB"')
    assert quiesce_idx < backup_idx

    quiesce_section = helper[
        helper.index("  quiesce)"):helper.index("  restore)")
    ]
    restore_section = helper[helper.index("  restore)"):]
    assert "weatheredge-strategy-analysis-cache.service" in quiesce_section
    assert '"${SYSTEMCTL[@]}" stop "$service"' in quiesce_section
    assert '"${SYSTEMCTL[@]}" reset-failed "$service"' in quiesce_section
    assert "weatheredge-strategy-analysis-cache.service" not in restore_section


def test_full_strategy_analysis_refresh_is_explicit_and_never_recurring():
    refresher = _read(AWS_DIR / "refresh_strategy_analysis_cache.sh")
    service = _read(AWS_DIR / "systemd" / "sfo-strategy-lab-refresh.service.in")
    deployer = _read(AWS_DIR / "sync_to_box.sh")

    assert "SFO_STRATEGY_FAST_PUBLICATION=0" in refresher
    assert "strategy_analysis_cache.json" in refresher
    assert "/etc/weatheredge.env" in refresher
    assert 'source "$ENV_FILE"' in refresher
    assert 'sudo -n cat -- "$ENV_FILE"' in refresher
    assert "systemd-run" in refresher
    assert "MemoryMax=1600M" in refresher
    assert "RuntimeMaxSec=" in refresher
    assert 'analysis_unit="weatheredge-strategy-analysis-cache.service"' in refresher
    assert 'sudo -n systemctl stop "$analysis_unit"' in refresher
    assert 'sudo -n systemctl reset-failed "$analysis_unit"' in refresher
    assert "SFO_STRATEGY_ANALYSIS_DB_PATH" in refresher
    assert "strategy_research_evidence.private.json" in refresher
    assert 'stage_evidence="$stage_dir/strategy_research_evidence.private.json"' in refresher
    assert "daily summary analysis is unavailable" in refresher
    assert refresher.index('mv -f -- "$evidence_promote_tmp"') < refresher.index(
        'mv -f -- "$promote_tmp"'
    )
    assert "analysis snapshot must differ from the live paper database" in refresher
    assert "os.path.samefile" in refresher
    assert "deployed build_info.json is missing source_sha" in refresher
    for cached_field in (
        "chronological_replay",
        "research_shadow",
        "forecast_scorecards",
        "daily_summary_analysis",
    ):
        assert cached_field in refresher
    assert refresher.index('source "$ENV_FILE"') < refresher.index(
        "SFO_STRATEGY_FAST_PUBLICATION=0"
    )
    assert "refresh_strategy_analysis_cache.sh" not in service
    assert "ANALYSIS_CACHE_REFRESHED=0" in deployer
    assert "continuing with deferred analysis" in deployer
    assert deployer.count(
        "sudo systemctl start sfo-strategy-lab-refresh.service"
    ) == 2
    assert deployer.count(
        "bash deploy/aws/wait_for_publication_manifest.sh"
    ) == 2
    post_analysis_idx = deployer.index(
        "if (( ANALYSIS_CACHE_REFRESHED == 1 ))"
    )
    post_analysis = deployer[post_analysis_idx:]
    assert (
        "systemctl stop sfo-strategy-lab-refresh.timer "
        "sfo-operational-publish.timer"
    ) in post_analysis
    assert "restore_post_analysis_timers" in post_analysis
    assert "POST_ANALYSIS_STATUS=0" in post_analysis
    assert "POST_ANALYSIS_RESTART_STATUS=0" in post_analysis
    post_restore_call = "if restore_post_analysis_timers; then"
    assert post_analysis.index("POST_ANALYSIS_STATUS=0") < post_analysis.index(
        post_restore_call
    )
    assert post_analysis.index(post_restore_call) < post_analysis.index(
        "if (( POST_ANALYSIS_STATUS != 0 ))"
    )


def test_publication_wait_exits_on_signals_and_cleans_on_exit():
    waiter = _read(AWS_DIR / "wait_for_publication_manifest.sh")

    assert (
        'TIMEOUT_SECONDS="${SFO_PUBLICATION_PROPAGATION_TIMEOUT_SECONDS:-420}"'
        in waiter
    )
    assert "trap cleanup EXIT" in waiter
    assert "trap 'exit 129' HUP" in waiter
    assert "trap 'exit 130' INT" in waiter
    assert "trap 'exit 143' TERM" in waiter
    assert "trap cleanup EXIT HUP INT TERM" not in waiter


def test_recurring_services_never_mutate_deployed_source():
    for unit in (AWS_DIR / "systemd").glob("*.service.in"):
        assert "sync_forecaster_source.sh" not in _read(unit), unit.name


def test_index_deploy_skips_disk_gate_when_definitions_are_current():
    indexer = _read(AWS_DIR / "create_decision_snapshot_index.sh")

    state_idx = indexer.index('if [[ "$index_state" == "current" ]]')
    exit_idx = indexer.index("exit 0", state_idx)
    disk_gate_idx = indexer.index("db_bytes=", exit_idx)
    assert state_idx < exit_idx < disk_gate_idx
    assert "SELECT name, sql FROM sqlite_master" in indexer
    assert 'DROP INDEX IF EXISTS "{name}"' in indexer


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


def test_full_forecaster_sync_uses_runtime_exclude_manifest():
    full_sync = _read(AWS_DIR / "sync_to_box.sh")
    excludes = _read(AWS_DIR / "forecaster-runtime.rsync-filter")

    assert 'FORECASTER_EXCLUDES="$SCRIPT_DIR/forecaster-runtime.rsync-filter"' in full_sync
    assert '--exclude-from="$FORECASTER_EXCLUDES"' in full_sync

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
    assert "--exclude '*.egg-info'" in syncer


def test_tracked_forecaster_inputs_are_copied_to_the_box():
    full_sync = _read(AWS_DIR / "sync_to_box.sh")

    for artifact in (
        "forecast_data.json",
        "weather_story_data.json",
    ):
        assert f'--exclude "{artifact}"' not in full_sync


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


def test_apple_weather_runtime_source_is_isolated_scheduled_and_expiry_purged():
    service = _read(AWS_DIR / "systemd" / "weatheredge-apple-refresh.service.in")
    timer = _read(AWS_DIR / "systemd" / "weatheredge-apple-refresh.timer")
    apple_purge_service = _read(
        AWS_DIR / "systemd" / "weatheredge-apple-purge.service.in"
    )
    apple_purge_timer = _read(
        AWS_DIR / "systemd" / "weatheredge-apple-purge.timer"
    )
    google_purge_service = _read(
        AWS_DIR / "systemd" / "weatheredge-google-runtime-purge.service.in"
    )
    deployer = _read(AWS_DIR / "sync_to_box.sh")
    installer = _read(AWS_DIR / "install_systemd.sh")
    notimers = _read(AWS_DIR / "install_systemd_notimers.sh")
    quiesce = _read(AWS_DIR / "disable_systemd_timers.sh")
    health = _read(AWS_DIR / "check_scheduler_health.sh")
    integrity = _read(AWS_DIR / "verify_systemd_unit_integrity.sh")
    env_example = _read(AWS_DIR / "sfo-weather.env.example")
    root_gitignore = _read(ROOT / ".gitignore")
    runtime_filter = _read(AWS_DIR / "forecaster-runtime.rsync-filter")
    tmpfiles = _read(AWS_DIR / "systemd" / "weatheredge-tmpfiles.conf")

    assert "apple_weatherkit.py --cities all" in service
    assert "OnFailure=sfo-alert@%n.service" in service
    assert "EnvironmentFile=__ENV_FILE__" in service
    assert "User=__APP_USER__" in service
    assert "OnCalendar=*-*-* 02,08,14,20:17:00 UTC" in timer
    assert "AccuracySec=1s" in timer
    assert "Unit=weatheredge-apple-refresh.service" in timer
    assert "apple_weatherkit.py --purge-only" in apple_purge_service
    assert "OnFailure=sfo-alert@%n.service" in apple_purge_service
    assert "OnCalendar=*:1/10" in apple_purge_timer
    assert "AccuracySec=1s" in apple_purge_timer
    assert "Unit=weatheredge-apple-purge.service" in apple_purge_timer
    assert "apple_weatherkit.py" not in google_purge_service

    for script in (installer, notimers):
        assert "weatheredge-apple-refresh.service.in" in script
        assert "weatheredge-apple-refresh.timer" in script
        assert "weatheredge-apple-purge.service.in" in script
        assert "weatheredge-apple-purge.timer" in script
    assert "weatheredge-apple-refresh.timer" in installer[
        installer.index("systemctl enable --now") :
    ]
    assert (
        "weatheredge-apple-refresh.timer weatheredge-apple-refresh.service"
        in quiesce
    )
    assert "weatheredge-apple-purge.timer weatheredge-apple-purge.service" in quiesce
    assert '"weatheredge-apple-refresh.timer"' in health
    assert '"weatheredge-apple-purge.timer"' in health
    assert '"weatheredge-apple-refresh.service"' in integrity
    assert '"weatheredge-apple-refresh.timer"' in integrity
    assert '"weatheredge-apple-purge.service"' in integrity
    assert '"weatheredge-apple-purge.timer"' in integrity
    assert "bash -s probe weatheredge-apple-refresh.timer" in deployer
    assert "bash -s probe weatheredge-apple-purge.timer" in deployer
    assert 'ENABLED_TIMERS+=("weatheredge-apple-refresh.timer")' in deployer
    assert 'ENABLED_TIMERS+=("weatheredge-apple-purge.timer")' in deployer
    assert "ENABLE_APPLE_WEATHER=0" in env_example
    assert "APPLE_WEATHER_PRIVATE_KEY_PATH=" in env_example
    assert "*.p8" in root_gitignore
    for private_key_glob in ("*.p8", "*.pem", "*.key"):
        assert private_key_glob in runtime_filter
        assert f"--exclude '{private_key_glob}'" in deployer
    assert "Apple Weather" in tmpfiles


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


def test_backup_sweep_precedes_the_free_space_measurement() -> None:
    helper = _read(AWS_DIR / "backup_paper_db.sh")
    sweep_idx = helper.index('-mtime "+$KEEP_DAYS" -delete')
    measure_idx = helper.index('database_bytes="$(wc -c < "$DB_PATH")"')
    preflight_exit_idx = helper.index('if [[ "$MODE" == "preflight" ]]; then')
    assert sweep_idx < measure_idx < preflight_exit_idx, (
        "the retention sweep must run before the space check, and before the "
        "preflight early-exit, or a preflight can never reclaim anything"
    )


def test_deploy_removes_the_verified_snapshot_after_the_analysis_refresh() -> None:
    deployer = _read(AWS_DIR / "sync_to_box.sh")
    analysis_idx = deployer.index("bash deploy/aws/refresh_strategy_analysis_cache.sh")
    removal_idx = deployer.index("rm -f -- '$ANALYSIS_DB_SNAPSHOT'")
    assert analysis_idx < removal_idx, (
        "the snapshot feeds the Strategy Lab refresh; it may only be removed "
        "after that step has consumed it"
    )


def test_deploy_removes_the_verified_snapshot_before_runtime_health_restoration() -> None:
    deployer = _read(AWS_DIR / "sync_to_box.sh")
    analysis_idx = deployer.index("bash deploy/aws/refresh_strategy_analysis_cache.sh")
    removal_idx = deployer.index("rm -f -- '$ANALYSIS_DB_SNAPSHOT'")
    producer_restore_idx = deployer.index(
        'bash -s restore "${PRODUCER_TIMERS[@]}" < "$QUIESCE_HELPER"'
    )
    freshness_idx = deployer.index(
        "sudo systemctl start sfo-forecast-freshness.service"
    )

    assert analysis_idx < removal_idx < producer_restore_idx < freshness_idx, (
        "the immutable snapshot must be consumed and removed while deployment "
        "maintenance still holds producers; otherwise a near-capacity snapshot "
        "can trip the restored runtime's disk-health gate"
    )
