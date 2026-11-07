"""Offline regressions for operational guards and CI trust boundaries."""

import os
import re
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
AWS = ROOT / "trading" / "deploy" / "aws"


def _executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


@pytest.mark.parametrize("unit", ["sfo-dataset-backfill.timer", "sfo-dataset-backfill.service"])
def test_compaction_refuses_active_dataset_writer(tmp_path, unit):
    _executable(tmp_path / "systemctl", f'#!/bin/bash\n[[ "${{@: -1}}" == "{unit}" ]]\n')
    result = subprocess.run(
        ["/bin/bash", str(AWS / "compact_paper_db.sh")],
        env={**os.environ, "PATH": f"{tmp_path}:/usr/bin:/bin", "SFO_KALSHI_DB": str(tmp_path / "absent.db")},
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert f"{unit} is active" in result.stderr
    assert "paper database not found" not in result.stderr


def test_fresh_trading_venv_uses_python3_without_undefined_variable(tmp_path):
    installer = (AWS / "install_systemd.sh").read_text()
    start = installer.index('if [[ ! -d "$TRADING_DIR/.venv" ]]')
    block = installer[start:installer.index('\nAPP_GROUP=', start)]
    log = tmp_path / "python.log"
    _executable(tmp_path / "python3", f'#!/bin/bash\nprintf "%s\\n" "$@" > "{log}"\n')
    env = {key: value for key, value in os.environ.items() if key != "WEATHEREDGE_VENV_PYTHON"}
    result = subprocess.run(
        ["/bin/bash", "-euc", block],
        env={**env, "PATH": f"{tmp_path}:/usr/bin:/bin", "TRADING_DIR": str(tmp_path)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == ["-m", "venv", str(tmp_path / ".venv")]


@pytest.mark.parametrize("required", [False, True])
def test_ci_explicit_semgrep_requirement_fails_without_implicit_ci(tmp_path, required):
    workflow = (ROOT / ".github/workflows/verify.yml").read_text()
    setting = re.search(r'^\s+(\w+_REQUIRE_SEMGREP): "1"$', workflow, re.M)
    assert setting is not None
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "run_semgrep.sh"
    script.write_text((ROOT / "scripts/run_semgrep.sh").read_text())
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "dirname").symlink_to("/usr/bin/dirname")
    _executable(bin_dir / "python3", '#!/bin/bash\nprintf "0.0\\n"\n')
    env = {key: value for key, value in os.environ.items() if key not in {"CI", "SFO_REQUIRE_SEMGREP", "WEATHEREDGE_REQUIRE_SEMGREP", "BASH_ENV"}}
    env["PATH"] = str(bin_dir)
    if required:
        env[setting[1]] = "1"
    result = subprocess.run(["/bin/bash", str(script)], env=env, capture_output=True, text=True)
    assert result.returncode == int(required), result.stderr
    assert ("required here" if required else "WARN: Semgrep CLI not found") in result.stderr


def test_pages_workflow_pins_every_action_to_full_sha():
    workflow = (AWS / "pages-deploy-workflow.yml").read_text()
    actions = re.findall(r'^\s+uses: (\S+)', workflow, re.M)
    assert len(actions) == 6
    assert all(re.fullmatch(r'[\w/-]+@[0-9a-f]{40}', action) for action in actions)


def test_heroui_secret_is_scoped_to_only_credential_and_setup_steps():
    workflow = (ROOT / ".github/workflows/verify.yml").read_text()
    web = workflow.split('\n  web:', 1)[1]
    before_steps, steps = web.split('\n    steps:', 1)
    assert "HEROUI_KEY" not in before_steps
    secret_steps = []
    for step in steps.split('      - name: ')[1:]:
        if 'HEROUI_KEY: ${{ secrets.HEROUI_KEY }}' in step:
            secret_steps.append(step.splitlines()[0])
            assert '\n        env:\n' in step
    assert secret_steps == ["Verify HeroUI Pro credential", "Install licensed HeroUI Pro artifacts"]


@pytest.mark.parametrize(("name", "deadline"), [("scan", 900), ("monitor", 600), ("settle", 1800)])
def test_paper_services_bound_hangs_with_generous_deadlines(name, deadline):
    service = (AWS / f"systemd/sfo-kalshi-paper-{name}.service.in").read_text()
    assert f"\nTimeoutStartSec={deadline}\n" in service
    assert "OnFailure=sfo-alert@%n.service" in service
    assert "Type=oneshot" in service
    if name == "scan":
        watchdog = (AWS / "check_scheduler_health.sh").read_text()
        assert 'SFO_SCHEDULER_SCAN_MAX_AGE_MINUTES:-20' in watchdog
        assert 2 * 300 < deadline < 20 * 60
    elif name == "monitor":
        assert deadline > 4 * 30 + 3 * 45 + 120  # lock attempts, delays and a full tick
    else:
        assert deadline == 30 * 60  # settlement's actual calendar cadence
