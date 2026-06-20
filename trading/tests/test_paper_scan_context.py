"""The 5-min paper scan must record forecast+probability context every tick.

Regression guard for the 2026-06-16 data outage and the 2026-06-20 portfolio
allocator migration: the scheduled scanner must have one full-context owner per
tick. Independent arbitrage/tail/analyze placement paths are diagnostics only;
AWS paper placement flows through ``portfolio-scan``.

This drives the REAL deploy script (run_paper_scan_profiles.sh) with a stubbed CLI
and asserts the invariant: exactly one ``portfolio-scan`` writes full context
per tick.
"""

import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "aws" / "run_paper_scan_profiles.sh"

# A stub standing in for `python -m sfo_kalshi_quant.cli ...`: it records the
# subcommand and whether it was told to skip context, then exits 0 (success).
_STUB = """#!/usr/bin/env bash
sub="?"; skip="no"
for a in "$@"; do
  case "$a" in
    portfolio-scan|arbitrage|tail-basket|analyze) sub="$a" ;;
    --skip-context-snapshots) skip="yes" ;;
  esac
done
echo "$sub skip=$skip" >> "$SCAN_CALL_LOG"
exit 0
"""


def _run_scan(tmp: Path, **env_overrides) -> list[tuple[str, bool]]:
    log = tmp / "calls.log"
    log.write_text("")
    stub = tmp / "py-stub.sh"
    stub.write_text(_STUB)
    stub.chmod(0o755)

    env = {
        **os.environ,
        "SCAN_CALL_LOG": str(log),
        "SFO_TRADING_PYTHON": str(stub),
        "SFO_TRADING_ROOT": str(tmp),
        "SFO_FORECASTER_ROOT": str(tmp),
        "SFO_KALSHI_DB": str(tmp / "x.db"),
        "SFO_PAPER_SCAN_LOCK": str(tmp / "scan.lock"),
        "PAPER_RISK_PROFILES": "live",  # single profile -> exactly one context write
        **env_overrides,
    }
    subprocess.run(["bash", str(SCRIPT)], env=env, check=True, capture_output=True)

    calls: list[tuple[str, bool]] = []
    for line in log.read_text().splitlines():
        sub, _, flag = line.partition(" skip=")
        calls.append((sub, flag == "yes"))
    return calls


def test_portfolio_scan_owns_context_and_old_placement_paths_do_not_run():
    if shutil.which("bash") is None:  # pragma: no cover - bash is present on CI/dev
        return
    with TemporaryDirectory() as t:
        calls = _run_scan(Path(t))

    assert calls, "scan invoked no commands"
    assert {sub for sub, _ in calls} == {"portfolio-scan"}
    owners = [sub for sub, skipped in calls if not skipped]
    assert owners == ["portfolio-scan"]


def test_second_profile_skips_duplicate_context():
    if shutil.which("bash") is None:  # pragma: no cover
        return
    with TemporaryDirectory() as t:
        calls = _run_scan(
            Path(t),
            PAPER_RISK_PROFILES="live,research",
        )

    owners = [sub for sub, skipped in calls if not skipped]
    assert owners == ["portfolio-scan"], f"expected one context owner, got {owners}"
    assert calls == [("portfolio-scan", False), ("portfolio-scan", True)]
