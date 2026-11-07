import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("setting", ["default", "supported", "override"])
def test_paper_analysis_selects_interpreter_and_forwards_profiles(tmp_path, setting):
    root = Path(__file__).resolve().parents[2]
    script = tmp_path / "scripts" / "paper_analyze.sh"
    script.parent.mkdir()
    shutil.copyfile(root / "scripts" / "paper_analyze.sh", script)
    default = tmp_path / "trading" / ".venv" / "bin" / "python"
    default.parent.mkdir(parents=True)
    supported = tmp_path / "supported-python"
    override = tmp_path / "override-python"
    for interpreter in (default, supported, override):
        interpreter.write_text('#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$CALL_LOG"\n')
        interpreter.chmod(0o755)
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTHON_BIN", "SFO_TRADING_PYTHON", "PAPER_RISK_PROFILE"}}
    log = tmp_path / "calls"
    env.update(CALL_LOG=str(log), PAPER_RISK_PROFILES="live, research", SFO_DISABLE_CLISFO="1")
    expected = "trading/.venv/bin/python"
    if setting != "default":
        env["SFO_TRADING_PYTHON"] = str(supported)
        expected = str(supported)
    if setting == "override":
        env["PYTHON_BIN"] = str(override)
        expected = str(override)
    result = subprocess.run(["bash", str(script), "--cities", "sfo"], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [
        f"{expected} -m sfo_kalshi_quant.cli --no-color --risk-profile {profile} "
        "analyze --target-date both --side both --cities sfo"
        for profile in ("live", "research")
    ]
