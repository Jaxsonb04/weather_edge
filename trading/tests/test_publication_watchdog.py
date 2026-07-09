from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sfo_kalshi_quant.publication import build_manifest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "trading" / "deploy" / "aws" / "check_forecast_db_freshness.sh"


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _fresh_root(
    parent: Path,
    *,
    operational_minutes: int = 1,
    signal_minutes: int | None = None,
    cities_minutes: int | None = None,
    strategy_minutes: int = 1,
) -> Path:
    now = datetime.now(timezone.utc)
    signal_age = operational_minutes if signal_minutes is None else signal_minutes
    cities_age = operational_minutes if cities_minutes is None else cities_minutes
    root = parent / "forecaster"
    root.mkdir()
    (root / "weather.db").write_bytes(b"sqlite-placeholder")
    _write_json(
        root / "trading_signal.json",
        {"generated_at": _iso(now - timedelta(minutes=signal_age))},
    )
    _write_json(
        root / "cities_data.json",
        {"generated_at": _iso(now - timedelta(minutes=cities_age))},
    )
    _write_json(root / "forecast_data.json", {"table": []})
    _write_json(root / "weather_story_data.json", {"temperature_histogram": {}})
    _write_json(
        root / "strategy_research.json",
        {
            "available": True,
            "generated_at": _iso(now - timedelta(minutes=strategy_minutes)),
        },
    )
    build_manifest(root, now=now)
    return root


def _run(root: Path, *, public_url: str = "") -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "SFO_FORECASTER_ROOT": str(root),
        "SFO_FORECAST_DB": str(root / "weather.db"),
        "SFO_FORECAST_MAX_AGE_HOURS": "6",
        "SFO_FORECAST_STALE_MARKER": str(root / "STALE_FORECAST"),
        "SFO_TRADING_ROOT": str(REPO_ROOT / "trading"),
        "SFO_TRADING_PYTHON": sys.executable,
        "SFO_PUBLICATION_MANIFEST_PATH": str(root / "publication_manifest.json"),
        "SFO_PUBLICATION_MAX_OPERATIONAL_AGE_MINUTES": "10",
        "SFO_PUBLICATION_MAX_STRATEGY_AGE_MINUTES": "20",
        "SFO_PUBLICATION_MANIFEST_URL": public_url,
        "SFO_FRESHNESS_ALERT_URL": "",
    }
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_watchdog_accepts_fresh_database_and_publication_manifest():
    with tempfile.TemporaryDirectory() as tmp:
        root = _fresh_root(Path(tmp))

        result = _run(root)

    assert result.returncode == 0, result.stderr
    assert "publication manifest valid" in result.stdout
    assert "forecast DB fresh" in result.stdout


def test_watchdog_rejects_stale_operational_artifacts_even_with_fresh_manifest():
    with tempfile.TemporaryDirectory() as tmp:
        root = _fresh_root(Path(tmp), operational_minutes=15)

        result = _run(root)

    assert result.returncode == 1
    assert "trading_signal.json is stale" in result.stderr


def test_watchdog_rejects_stale_cities_with_fresh_signal_and_manifest():
    with tempfile.TemporaryDirectory() as tmp:
        root = _fresh_root(Path(tmp), signal_minutes=1, cities_minutes=15)

        result = _run(root)

    assert result.returncode == 1
    assert "cities_data.json is stale" in result.stderr
    assert "trading_signal.json is stale" not in result.stderr


def test_watchdog_rejects_stale_or_missing_strategy_research():
    with tempfile.TemporaryDirectory() as tmp:
        root = _fresh_root(Path(tmp), strategy_minutes=25)

        stale = _run(root)

    assert stale.returncode == 1
    assert "strategy_research.json is stale" in stale.stderr

    with tempfile.TemporaryDirectory() as tmp:
        root = _fresh_root(Path(tmp))
        (root / "strategy_research.json").unlink()

        missing = _run(root)

    assert missing.returncode == 1
    assert "strategy" in missing.stderr


def test_watchdog_rejects_missing_or_invalid_local_manifest():
    with tempfile.TemporaryDirectory() as tmp:
        root = _fresh_root(Path(tmp))
        (root / "publication_manifest.json").unlink()

        missing = _run(root)

    assert missing.returncode == 1
    assert "manifest" in missing.stderr

    with tempfile.TemporaryDirectory() as tmp:
        root = _fresh_root(Path(tmp))
        (root / "publication_manifest.json").write_text("{not-json", encoding="utf-8")

        invalid = _run(root)

    assert invalid.returncode == 1
    assert "invalid JSON" in invalid.stderr


def test_watchdog_optionally_rejects_stale_public_manifest():
    with tempfile.TemporaryDirectory() as tmp:
        parent = Path(tmp)
        root = _fresh_root(parent)
        public_manifest = parent / "public_manifest.json"
        payload = json.loads((root / "publication_manifest.json").read_text(encoding="utf-8"))
        stale = datetime.now(timezone.utc) - timedelta(minutes=30)
        payload["artifacts"]["cities_data.json"]["generated_at"] = _iso(stale)
        public_manifest.write_text(json.dumps(payload), encoding="utf-8")

        result = _run(root, public_url=public_manifest.as_uri())

    assert result.returncode == 1
    assert "public publication manifest" in result.stderr
    assert "cities_data.json is stale" in result.stderr
    assert "trading_signal.json is stale" not in result.stderr
