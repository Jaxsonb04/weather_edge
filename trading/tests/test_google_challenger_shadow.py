"""Task 7: SFO shadow dual-run + paired baseline/Google-challenger evidence.

Covers the three numbered Task 7 requirements:

1. Persistence -- ``PaperStore.record_google_challenger_snapshot`` stores
   ONLY derived baseline/challenger probabilities and mu/sigma/action, and
   fails closed on any raw Google field name (highF, gap, raw, url, key,
   token, conditions, body, response) appearing as a probability key.
2. Shadow proof -- ``run_sfo_google_shadow`` calls the SFO serve path
   (``SfoForecasterAdapter.latest_blend``) exactly once and returns that
   object byte-identical to calling ``latest_blend`` directly; running it
   never mutates the permanent EMOS baseline either.
3. Isolation -- neither ``google_challenger_shadow`` nor the forecaster-side
   ``google_paired_evidence``/``google_runtime_blend`` modules are imported
   by the live decision-recording/trading-loop modules.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.forecast import SfoForecasterAdapter
from sfo_kalshi_quant.google_challenger_shadow import (
    build_google_challenger_snapshot,
    run_sfo_google_shadow,
)
from sfo_kalshi_quant.models import GoogleChallengerSnapshot, MarketBin

TARGET = date(2026, 7, 19)


def _bin(ticker: str, strike_type: str, floor: float | None, cap: float | None) -> MarketBin:
    return MarketBin(
        ticker=ticker,
        event_ticker="KXHIGHTSFO-26JUL19",
        title="",
        yes_sub_title="",
        strike_type=strike_type,
        floor_strike=floor,
        cap_strike=cap,
        yes_bid=0.0,
        yes_ask=1.0,
        no_bid=0.0,
        no_ask=1.0,
        yes_bid_size=0.0,
        yes_ask_size=0.0,
        status="active",
    )


_MARKETS = (
    _bin("KXHIGHTSFO-26JUL19-T80", "less", None, 80),
    _bin("KXHIGHTSFO-26JUL19-B80-82", "between", 80, 82),
    _bin("KXHIGHTSFO-26JUL19-T82", "greater", 82, None),
)


def _seed_weather_db(root: Path, *, emos_mu=80.0, emos_sigma=3.0, challenger_mu=80.45,
                      challenger_sigma=3.0, action="forecast", blend_row=True) -> None:
    db_path = root / "weather.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE forecast_blend_daily_high (
                fetched_at TEXT NOT NULL,
                target_date TEXT NOT NULL,
                lead_hours REAL,
                method TEXT NOT NULL,
                predicted_high_f REAL NOT NULL,
                google_high_f REAL,
                nws_high_f REAL,
                open_meteo_high_f REAL,
                history_high_f REAL,
                google_weight REAL,
                nws_weight REAL,
                open_meteo_weight REAL,
                history_weight REAL,
                station_adjustment_f REAL,
                fresh_station_count INTEGER,
                source_count INTEGER,
                time_zone TEXT,
                max_calls_per_day INTEGER,
                calls_used_today INTEGER,
                details_json TEXT,
                actual_high_f REAL,
                abs_error_f REAL,
                scored_at TEXT,
                PRIMARY KEY (fetched_at, target_date)
            )
            """
        )
        if blend_row:
            conn.execute(
                """
                INSERT INTO forecast_blend_daily_high (
                    fetched_at, target_date, lead_hours, method, predicted_high_f,
                    source_count
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("2026-07-18T22:00:00+00:00", TARGET.isoformat(), 20.0, "weighted blend", 81.2, 4),
            )
        conn.execute(
            """
            CREATE TABLE forecast_emos_daily_high (
                station_id TEXT NOT NULL,
                target_date TEXT NOT NULL,
                predicted_high_f REAL NOT NULL,
                sigma_f REAL,
                n_models INTEGER,
                model_spread_f REAL,
                fetched_at TEXT,
                method TEXT,
                lead_days INTEGER,
                source TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO forecast_emos_daily_high (
                station_id, target_date, predicted_high_f, sigma_f, n_models,
                model_spread_f, fetched_at, method, lead_days, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("KSFO", TARGET.isoformat(), emos_mu, emos_sigma, 4, 1.5,
             "2026-07-18T21:00:00+00:00", "emos", 1, "live"),
        )
        conn.execute(
            """
            CREATE TABLE google_challenger_research_baseline (
                station_id TEXT NOT NULL,
                target_date TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                baseline_mu REAL NOT NULL,
                baseline_sigma REAL NOT NULL,
                challenger_mu REAL,
                challenger_sigma REAL NOT NULL,
                action TEXT NOT NULL,
                PRIMARY KEY(station_id, target_date, issued_at, policy_version)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO google_challenger_research_baseline (
                station_id, target_date, issued_at, policy_version,
                baseline_mu, baseline_sigma, challenger_mu, challenger_sigma, action
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("KSFO", TARGET.isoformat(), "2026-07-18T19:00:00+00:00",
             "google-runtime-fixed-v1", emos_mu, emos_sigma, challenger_mu,
             challenger_sigma, action),
        )


# ---------------------------------------------------------------------------
# Requirement 2: SFO shadow proof.
# ---------------------------------------------------------------------------


def _asdict_json(snapshot) -> str:
    return json.dumps(dataclasses.asdict(snapshot), sort_keys=True, default=str)


def test_served_forecast_is_byte_identical_with_shadow_disabled_and_enabled(tmp_path):
    _seed_weather_db(tmp_path)
    adapter = SfoForecasterAdapter(tmp_path)
    store = PaperStore(tmp_path / "paper.db")

    disabled = adapter.latest_blend(TARGET)
    served, shadow = run_sfo_google_shadow(
        adapter, TARGET, paper_store=store, markets=_MARKETS
    )

    assert served == disabled
    assert _asdict_json(served) == _asdict_json(disabled)
    assert shadow is not None
    # W3 (Task 7 review, optional hardening): equality/JSON-equality between
    # the two arms alone would not catch a regression that moved BOTH arms
    # away from the correct value together -- pin the exact value seeded in
    # forecast_blend_daily_high (predicted_high_f=81.2) in both arms.
    assert served.predicted_high_f == pytest.approx(81.2)
    assert disabled.predicted_high_f == pytest.approx(81.2)


def test_shadow_dual_run_never_mutates_the_permanent_emos_baseline(tmp_path):
    _seed_weather_db(tmp_path)
    adapter = SfoForecasterAdapter(tmp_path)
    store = PaperStore(tmp_path / "paper.db")

    before = adapter.latest_emos_snapshot(TARGET)
    run_sfo_google_shadow(adapter, TARGET, paper_store=store, markets=_MARKETS)
    after = adapter.latest_emos_snapshot(TARGET)

    assert before == after
    assert _asdict_json(before) == _asdict_json(after)


def test_shadow_run_returns_none_snapshot_when_no_paired_evidence_exists(tmp_path):
    _seed_weather_db(tmp_path)
    # Drop the paired-evidence table entirely -- e.g. Google was unavailable.
    with sqlite3.connect(tmp_path / "weather.db") as conn:
        conn.execute("DROP TABLE google_challenger_research_baseline")
    adapter = SfoForecasterAdapter(tmp_path)
    store = PaperStore(tmp_path / "paper.db")

    served, shadow = run_sfo_google_shadow(
        adapter, TARGET, paper_store=store, markets=_MARKETS
    )

    assert served == adapter.latest_blend(TARGET)
    assert shadow is None
    with store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM google_challenger_snapshots").fetchone()[0]
    assert count == 0


def test_challenger_follows_the_fixed_formula(tmp_path):
    # gap = 83 - 80 = 3F -> mu = 80 + 0.15*3 = 80.45 (< 7F, not blocked).
    _seed_weather_db(tmp_path, emos_mu=80.0, emos_sigma=3.0, challenger_mu=80.45,
                      challenger_sigma=3.0, action="forecast")
    adapter = SfoForecasterAdapter(tmp_path)

    snapshot = build_google_challenger_snapshot(adapter, TARGET, markets=_MARKETS)

    assert snapshot is not None
    assert snapshot.baseline_mu == 80.0
    assert snapshot.challenger_mu == pytest.approx(80.45)
    assert snapshot.action == "forecast"


def test_blocked_action_persists_no_challenger_mean_or_probabilities(tmp_path):
    _seed_weather_db(
        tmp_path, emos_mu=80.0, emos_sigma=3.0, challenger_mu=None,
        challenger_sigma=3.0, action="external_runtime_corroboration_block",
    )
    adapter = SfoForecasterAdapter(tmp_path)

    snapshot = build_google_challenger_snapshot(adapter, TARGET, markets=_MARKETS)

    assert snapshot is not None
    assert snapshot.action == "external_runtime_corroboration_block"
    assert snapshot.challenger_mu is None
    assert snapshot.challenger_probabilities is None
    assert snapshot.baseline_probabilities  # baseline is always priced


# ---------------------------------------------------------------------------
# Requirement 1: persistence -- only derived probabilities, raw fields
# rejected.
# ---------------------------------------------------------------------------


def test_persisted_snapshot_contains_only_bracket_probabilities(tmp_path):
    _seed_weather_db(tmp_path)
    adapter = SfoForecasterAdapter(tmp_path)
    store = PaperStore(tmp_path / "paper.db")

    run_sfo_google_shadow(adapter, TARGET, paper_store=store, markets=_MARKETS)

    with store.connect() as conn:
        row = conn.execute(
            "SELECT baseline_probabilities_json, challenger_probabilities_json, "
            "baseline_mu, baseline_sigma, challenger_mu, challenger_sigma, action "
            "FROM google_challenger_snapshots"
        ).fetchone()
    assert row is not None
    baseline_probabilities = json.loads(row[0])
    challenger_probabilities = json.loads(row[1])
    # Exact key-set equality is the precise check: the persisted payload
    # contains ONLY the bracket tickers priced above, nothing extra (a raw
    # Google field name substring scan would false-positive on legitimate
    # ticker text like "KXHIGHTSFO", which itself contains "high").
    assert set(baseline_probabilities) == {market.ticker for market in _MARKETS}
    assert set(challenger_probabilities) == {market.ticker for market in _MARKETS}
    assert all(0.0 <= value <= 1.0 for value in baseline_probabilities.values())
    assert all(isinstance(value, float) for value in baseline_probabilities.values())


def test_record_google_challenger_snapshot_rejects_a_raw_google_field_key(tmp_path):
    store = PaperStore(tmp_path / "paper.db")
    snapshot = GoogleChallengerSnapshot(
        station_id="KSFO",
        target_date=TARGET,
        issued_at="2026-07-18T19:00:00+00:00",
        policy_version="google-runtime-fixed-v1",
        baseline_mu=80.0,
        baseline_sigma=3.0,
        challenger_mu=80.45,
        challenger_sigma=3.0,
        baseline_probabilities={"google_high_f": 0.5, "KXHIGHTSFO-TEST-T80": 0.5},
        challenger_probabilities=None,
        action="forecast",
    )

    with pytest.raises(ValueError):
        store.record_google_challenger_snapshot(snapshot)

    with store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM google_challenger_snapshots").fetchone()[0]
    assert count == 0


def test_record_google_challenger_snapshot_rejects_an_out_of_range_probability(tmp_path):
    store = PaperStore(tmp_path / "paper.db")
    snapshot = GoogleChallengerSnapshot(
        station_id="KSFO",
        target_date=TARGET,
        issued_at="2026-07-18T19:00:00+00:00",
        policy_version="google-runtime-fixed-v1",
        baseline_mu=80.0,
        baseline_sigma=3.0,
        challenger_mu=80.45,
        challenger_sigma=3.0,
        baseline_probabilities={"KXHIGHTSFO-TEST-T80": 1.5},
        challenger_probabilities=None,
        action="forecast",
    )

    with pytest.raises(ValueError):
        store.record_google_challenger_snapshot(snapshot)


def test_record_google_challenger_snapshot_persists_a_valid_row(tmp_path):
    store = PaperStore(tmp_path / "paper.db")
    snapshot = GoogleChallengerSnapshot(
        station_id="KSFO",
        target_date=TARGET,
        issued_at="2026-07-18T19:00:00+00:00",
        policy_version="google-runtime-fixed-v1",
        baseline_mu=80.0,
        baseline_sigma=3.0,
        challenger_mu=80.45,
        challenger_sigma=3.0,
        baseline_probabilities={"KXHIGHTSFO-TEST-T80": 0.4, "KXHIGHTSFO-TEST-T82": 0.6},
        challenger_probabilities={"KXHIGHTSFO-TEST-T80": 0.35, "KXHIGHTSFO-TEST-T82": 0.65},
        action="forecast",
    )

    store.record_google_challenger_snapshot(snapshot)

    with store.connect() as conn:
        row = conn.execute(
            "SELECT station_id, target_date, action FROM google_challenger_snapshots"
        ).fetchone()
    assert row == ("KSFO", TARGET.isoformat(), "forecast")


def test_record_google_challenger_snapshot_is_replay_safe_on_the_same_identity(tmp_path):
    """W1 (Task 7 review, HIGH): a plain INSERT crashes with sqlite3.IntegrityError
    on the second cadence cycle that derives the same (station_id, target_date,
    issued_at, policy_version) identity -- e.g. a retried refresh or a second
    orchestrator pass inside the same Google issue window. Replaying the exact
    same snapshot must be a safe no-op, not a crash.
    """

    store = PaperStore(tmp_path / "paper.db")
    snapshot = GoogleChallengerSnapshot(
        station_id="KSFO",
        target_date=TARGET,
        issued_at="2026-07-18T19:00:00+00:00",
        policy_version="google-runtime-fixed-v1",
        baseline_mu=80.0,
        baseline_sigma=3.0,
        challenger_mu=80.45,
        challenger_sigma=3.0,
        baseline_probabilities={"KXHIGHTSFO-TEST-T80": 0.4, "KXHIGHTSFO-TEST-T82": 0.6},
        challenger_probabilities={"KXHIGHTSFO-TEST-T80": 0.35, "KXHIGHTSFO-TEST-T82": 0.65},
        action="forecast",
    )

    store.record_google_challenger_snapshot(snapshot)
    # Replaying the identical identity/payload a second (and third) time must
    # not raise -- a retried refresh cycle re-deriving the same evidence is
    # expected operational behavior, not an error.
    store.record_google_challenger_snapshot(snapshot)
    store.record_google_challenger_snapshot(snapshot)

    with store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM google_challenger_snapshots").fetchone()[0]
        row = conn.execute(
            "SELECT station_id, target_date, action FROM google_challenger_snapshots"
        ).fetchone()
    assert count == 1
    assert row == ("KSFO", TARGET.isoformat(), "forecast")


def test_record_google_challenger_snapshot_rejects_a_conflicting_replay(tmp_path):
    """W1: a collision on the same identity with a DIFFERENT payload is a real
    data-integrity problem (the same issued evidence must never be silently
    overwritten or silently ignored when it actually changed) -- fail loudly
    rather than accept whichever payload happened to write first.
    """

    store = PaperStore(tmp_path / "paper.db")
    first = GoogleChallengerSnapshot(
        station_id="KSFO",
        target_date=TARGET,
        issued_at="2026-07-18T19:00:00+00:00",
        policy_version="google-runtime-fixed-v1",
        baseline_mu=80.0,
        baseline_sigma=3.0,
        challenger_mu=80.45,
        challenger_sigma=3.0,
        baseline_probabilities={"KXHIGHTSFO-TEST-T80": 0.4, "KXHIGHTSFO-TEST-T82": 0.6},
        challenger_probabilities={"KXHIGHTSFO-TEST-T80": 0.35, "KXHIGHTSFO-TEST-T82": 0.65},
        action="forecast",
    )
    conflicting = dataclasses.replace(first, baseline_mu=81.0)

    store.record_google_challenger_snapshot(first)

    with pytest.raises(ValueError):
        store.record_google_challenger_snapshot(conflicting)

    with store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM google_challenger_snapshots").fetchone()[0]
        row = conn.execute(
            "SELECT baseline_mu FROM google_challenger_snapshots"
        ).fetchone()
    assert count == 1
    assert row == (80.0,)


# ---------------------------------------------------------------------------
# Requirement 3: structural isolation.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIVE_LOOP_MODULES = tuple(
    _REPO_ROOT / "trading" / "sfo_kalshi_quant" / relative
    for relative in (
        "edge_scan.py",
        "paper.py",
        "live_execution.py",
        "monitor.py",
        "forecast.py",
        "db.py",
        "models.py",
        "prediction_features.py",
        "report.py",
        "publication.py",
        "store/diagnostics.py",
        "store/schema.py",
    )
)


def test_no_live_trading_module_imports_the_shadow_orchestrator_or_google_modules():
    for path in _LIVE_LOOP_MODULES:
        assert path.is_file(), path
        source = path.read_text()
        assert "google_challenger_shadow" not in source
        assert "google_runtime_blend" not in source
        assert "google_paired_evidence" not in source


def test_shadow_orchestrator_never_imports_the_live_trading_loop():
    """The shadow module composes forecast/db/models/prediction_features
    (already-reviewed persistence surfaces) but must never import the actual
    live trading loop (edge_scan/paper.py/live_execution). Checks for the
    module names themselves rather than a bare "paper" substring, since
    ``PaperStore`` (the persistence class this module legitimately imports
    from db.py) also contains that substring.
    """

    source = (
        _REPO_ROOT / "trading" / "sfo_kalshi_quant" / "google_challenger_shadow.py"
    ).read_text()
    for forbidden in ("edge_scan", "live_execution", "from .paper import", "import paper"):
        assert forbidden not in source
