"""The nightly dataset backfill must tolerate one bad night per source.

Upstream providers (IEM, NOMADS, Open-Meteo) return transient 5xx often enough
that failing the unit on a single miss produces alerts nobody can act on. The
backfill window is a rolling lookback, so the next run re-covers the missed
dates and only a source failing on consecutive runs is a real outage.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "trading" / "deploy" / "aws" / "run_dataset_backfill.sh"


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _run(
    tmp_path: Path,
    *,
    failing_sources: str,
    sources: str = "iem-asos,gfs-mos",
    threshold: str = "2",
) -> subprocess.CompletedProcess[str]:
    """Run the backfill with a stub CLI that fails the named sources."""
    trading = tmp_path / "trading"
    trading.mkdir(exist_ok=True)

    # Stand in for `python -m sfo_kalshi_quant.cli`: fail whenever the
    # requested --source is in the failing set, and succeed for everything
    # else, including the trailing dataset-research invocation.
    python_stub = tmp_path / "python-stub"
    _write_executable(
        python_stub,
        f"""#!/usr/bin/env bash
set -uo pipefail
if [[ "$1" == "-c" ]]; then
  # The script asks Python for a monotonic clock and for date arithmetic.
  if [[ "$2" == *monotonic* ]]; then
    echo "0"
  else
    echo "2026-08-01"
  fi
  exit 0
fi
source_name=""
prev=""
for arg in "$@"; do
  if [[ "$prev" == "--source" ]]; then
    source_name="$arg"
  fi
  prev="$arg"
done
if [[ -n "$source_name" ]]; then
  failing_list=()
  IFS=',' read -r -a failing_list <<< "{failing_sources}"
  for failing in ${{failing_list[@]+"${{failing_list[@]}}"}}; do
    if [[ -n "$failing" && "$source_name" == "$failing" ]]; then
      echo "stub failure for $source_name" >&2
      exit 1
    fi
  done
fi
exit 0
""",
    )

    env = {
        **os.environ,
        "SFO_TRADING_ROOT": str(trading),
        "SFO_TRADING_PYTHON": str(python_stub),
        "SFO_DATASET_DB": str(tmp_path / "data" / "paper_trading.db"),
        "SFO_DATASET_STATE_DIR": str(tmp_path / "state"),
        "SFO_DATASET_SOURCES": sources,
        "SFO_DATASET_SOURCE_FAILURE_THRESHOLD": threshold,
        "SFO_DATASET_START_DATE": "2026-08-01",
        "SFO_DATASET_END_DATE": "2026-08-08",
        "SFO_FORECASTER_ROOT": str(tmp_path / "forecaster"),
        "SFO_DATASET_RESEARCH_PATH": str(tmp_path / "research.json"),
    }
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_backfill_succeeds_when_every_source_succeeds(tmp_path: Path) -> None:
    result = _run(tmp_path, failing_sources="")

    assert result.returncode == 0, result.stderr


def test_backfill_tolerates_a_single_failed_run_for_a_source(tmp_path: Path) -> None:
    result = _run(tmp_path, failing_sources="iem-asos")

    assert result.returncode == 0, result.stderr
    assert "failed 1/2 consecutive run(s)" in result.stderr
    assert "will be retried next run" in result.stderr
    state = tmp_path / "state" / "consecutive-failures-iem-asos"
    assert state.read_text(encoding="utf-8").strip() == "1"


def test_backfill_fails_when_a_source_fails_consecutive_runs(tmp_path: Path) -> None:
    first = _run(tmp_path, failing_sources="iem-asos")
    assert first.returncode == 0, first.stderr

    second = _run(tmp_path, failing_sources="iem-asos")

    assert second.returncode == 1
    assert "2 or more consecutive runs" in second.stderr
    assert "iem-asos(2)" in second.stderr


def test_backfill_resets_the_counter_after_a_source_recovers(tmp_path: Path) -> None:
    failed = _run(tmp_path, failing_sources="iem-asos")
    assert failed.returncode == 0, failed.stderr

    recovered = _run(tmp_path, failing_sources="")
    assert recovered.returncode == 0, recovered.stderr
    assert not (tmp_path / "state" / "consecutive-failures-iem-asos").exists()

    # A later isolated failure starts counting from one again.
    again = _run(tmp_path, failing_sources="iem-asos")
    assert again.returncode == 0, again.stderr
    assert "failed 1/2 consecutive run(s)" in again.stderr


def test_backfill_tracks_each_source_independently(tmp_path: Path) -> None:
    first = _run(tmp_path, failing_sources="iem-asos")
    assert first.returncode == 0, first.stderr

    second = _run(tmp_path, failing_sources="gfs-mos")

    assert second.returncode == 0, second.stderr
    assert not (tmp_path / "state" / "consecutive-failures-iem-asos").exists()
    assert (
        tmp_path / "state" / "consecutive-failures-gfs-mos"
    ).read_text(encoding="utf-8").strip() == "1"
