"""The 5-min paper scan must record forecast+probability context every tick.

Regression guard for the 2026-06-16 data outage: the scan runs arbitrage first,
but ``_arbitrage_one_target`` records ONLY market_snapshots (no forecast /
probability ladder). When a successful arbitrage run flipped the per-tick
"context already written" flag, the full-context commands (tail-basket / analyze)
were told to ``--skip-context-snapshots`` and wrote nothing -- silently starving
forecast_snapshots + probability_snapshots (and the dashboard calibration + the
legacy stop-loss model-veto that read them).

This drives the REAL deploy script (run_paper_scan_profiles.sh) with a stubbed CLI
and asserts the invariant: arbitrage never owns context, and exactly one
full-context command writes it per tick.
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
    arbitrage|tail-basket|analyze) sub="$a" ;;
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


def test_arbitrage_never_owns_context_and_full_context_is_written_once():
    if shutil.which("bash") is None:  # pragma: no cover - bash is present on CI/dev
        return
    with TemporaryDirectory() as t:
        calls = _run_scan(
            Path(t),
            SFO_PAPER_SCAN_ARBITRAGE_ENABLED="1",
            SFO_PAPER_SCAN_TAIL_BASKET_ENABLED="1",
        )

    assert calls, "scan invoked no commands"
    # Arbitrage records only market_snapshots, so it must ALWAYS skip context.
    for sub, skipped in calls:
        if sub == "arbitrage":
            assert skipped, "arbitrage must never be the per-tick context writer"
    # Exactly one full-context command writes context this tick, and it is not arbitrage.
    owners = [sub for sub, skipped in calls if not skipped]
    assert len(owners) == 1, f"expected exactly one context writer, got {owners}"
    assert owners[0] in ("tail-basket", "analyze")


def test_analyzer_owns_context_when_tail_basket_is_disabled():
    """The always-run analyzer is the fallback owner if the basket is off."""
    if shutil.which("bash") is None:  # pragma: no cover
        return
    with TemporaryDirectory() as t:
        calls = _run_scan(
            Path(t),
            SFO_PAPER_SCAN_ARBITRAGE_ENABLED="1",
            SFO_PAPER_SCAN_TAIL_BASKET_ENABLED="0",
        )

    owners = [sub for sub, skipped in calls if not skipped]
    assert owners == ["analyze"], f"expected analyze to own context, got {owners}"
