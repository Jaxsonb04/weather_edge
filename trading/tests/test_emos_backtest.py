"""Backtest emos_lookup seam + EMOS adapter reader (Phase 2 integration points)."""

import sqlite3
import tempfile
from datetime import date, timedelta
from pathlib import Path

from sfo_kalshi_quant.backtest import run_walk_forward_calibration_backtest
from sfo_kalshi_quant.config import StrategyConfig
from sfo_kalshi_quant.forecast import SfoForecasterAdapter
from sfo_kalshi_quant.models import ForecastOutcome


def _outcomes():
    start = date(2025, 1, 1)
    rows = []
    for idx in range(220):
        pred = 66.0 + (idx % 10) * 0.7
        residual = [-3, -2, -1, 0, 1, 2, 3, 4, -1, 1][idx % 10]
        rows.append(ForecastOutcome(local_date=start + timedelta(days=idx), predicted_high_f=pred, actual_high_f=pred + residual))
    return rows


def test_backtest_consumes_date_keyed_emos_lookup_and_documents_keytype_trap():
    outs = _outcomes()
    cfg = StrategyConfig(min_conditional_samples=20, emos_distribution_enabled=True)
    off = run_walk_forward_calibration_backtest(outs, config=cfg, min_train=180)

    # A date-keyed lookup (the contract) is actually consumed -> Brier changes.
    date_keyed = {o.local_date: (o.predicted_high_f + 6.0, 3.0) for o in outs}
    on = run_walk_forward_calibration_backtest(outs, config=cfg, min_train=180, emos_lookup=date_keyed)
    assert abs(on.brier_score - off.brier_score) > 1e-6

    # A str-keyed lookup violates the date-key contract: .get(date) misses every
    # day and the run silently reverts to the residual path (== no lookup). This
    # characterizes the trap so a loader returning str keys can't masquerade as an
    # EMOS run -- the exact silent-false-validation the review flagged.
    str_keyed = {o.local_date.isoformat(): (o.predicted_high_f + 6.0, 3.0) for o in outs}
    on_str = run_walk_forward_calibration_backtest(outs, config=cfg, min_train=180, emos_lookup=str_keyed)
    assert abs(on_str.brier_score - off.brier_score) < 1e-12


def _write_emos_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE forecast_emos_daily_high "
        "(target_date TEXT, lead_days INTEGER, predicted_high_f REAL, sigma_f REAL, "
        " n_models INTEGER, fetched_at TEXT, method TEXT, source TEXT, actual_high_f REAL, "
        " PRIMARY KEY (target_date, lead_days, source))"
    )
    conn.execute("INSERT INTO forecast_emos_daily_high VALUES ('2025-06-01',1,70.0,2.5,8,'x','m','s',NULL)")
    conn.execute("INSERT INTO forecast_emos_daily_high VALUES ('2025-06-01',2,99.0,9.0,8,'x','m','s',NULL)")
    conn.commit()
    conn.close()


def test_load_emos_mu_sigma_is_date_keyed_and_lead_filtered():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_emos_db(root / "weather.db")
        adapter = SfoForecasterAdapter(root=root)
        loaded = adapter.load_emos_mu_sigma(lead_days=1)
        # date-keyed (not str) and excludes the lead-2 row
        assert loaded == {date(2025, 6, 1): (70.0, 2.5)}


def test_load_emos_mu_sigma_reads_all_leads_when_lead_none():
    # The live serve writes each rolling target at its TRUE lead (next-day -> 1,
    # the 2-day-out market -> 2) on DISTINCT dates. lead_days=None must return
    # BOTH, keyed by target_date, so the live trader sees the 2-day-out market;
    # the default lead_days=1 read sees only the next-day market.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        conn = sqlite3.connect(root / "weather.db")
        conn.execute(
            "CREATE TABLE forecast_emos_daily_high "
            "(target_date TEXT, lead_days INTEGER, predicted_high_f REAL, sigma_f REAL, "
            " n_models INTEGER, fetched_at TEXT, method TEXT, source TEXT, actual_high_f REAL, "
            " PRIMARY KEY (target_date, lead_days, source))"
        )
        conn.execute("INSERT INTO forecast_emos_daily_high VALUES ('2026-06-28',1,68.0,2.5,8,'t','m','live',NULL)")
        conn.execute("INSERT INTO forecast_emos_daily_high VALUES ('2026-06-29',2,71.0,3.5,8,'t','m','live',NULL)")
        conn.commit()
        conn.close()
        adapter = SfoForecasterAdapter(root=root)
        assert adapter.load_emos_mu_sigma(lead_days=None) == {
            date(2026, 6, 28): (68.0, 2.5),
            date(2026, 6, 29): (71.0, 3.5),
        }
        # The previous fixed-lead-1 read silently dropped the 2-day-out market.
        assert adapter.load_emos_mu_sigma(lead_days=1) == {date(2026, 6, 28): (68.0, 2.5)}


def test_load_emos_mu_sigma_filters_by_source_and_prefers_freshest():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        conn = sqlite3.connect(root / "weather.db")
        conn.execute(
            "CREATE TABLE forecast_emos_daily_high "
            "(target_date TEXT, lead_days INTEGER, predicted_high_f REAL, sigma_f REAL, "
            " n_models INTEGER, fetched_at TEXT, method TEXT, source TEXT, actual_high_f REAL, "
            " PRIMARY KEY (target_date, lead_days, source))"
        )
        # Two sources for the SAME (date, lead); 'live' fetched later than 'rolling_origin'.
        conn.execute("INSERT INTO forecast_emos_daily_high VALUES ('2025-06-01',1,70.0,2.5,8,'2025-06-01T00:00','m','rolling_origin',72)")
        conn.execute("INSERT INTO forecast_emos_daily_high VALUES ('2025-06-01',1,99.0,9.0,8,'2025-06-02T00:00','m','live',NULL)")
        conn.commit()
        conn.close()
        adapter = SfoForecasterAdapter(root=root)
        # source filter is exact -> rescore reads can demand the leakage-safe rows
        assert adapter.load_emos_mu_sigma(source="rolling_origin") == {date(2025, 6, 1): (70.0, 2.5)}
        assert adapter.load_emos_mu_sigma(source="live") == {date(2025, 6, 1): (99.0, 9.0)}
        # unfiltered -> deterministic freshest-wins (live @06-02 beats rolling @06-01)
        assert adapter.load_emos_mu_sigma() == {date(2025, 6, 1): (99.0, 9.0)}


def test_load_emos_mu_sigma_prefers_live_over_newer_rolling_origin():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with sqlite3.connect(root / "weather.db") as conn:
            conn.execute(
                "CREATE TABLE forecast_emos_daily_high "
                "(target_date TEXT, lead_days INTEGER, predicted_high_f REAL, sigma_f REAL, "
                " n_models INTEGER, fetched_at TEXT, method TEXT, source TEXT, actual_high_f REAL, "
                " PRIMARY KEY (target_date, lead_days, source))"
            )
            conn.execute("INSERT INTO forecast_emos_daily_high VALUES ('2025-06-01',1,71.0,2.0,8,'2025-06-01T00:00','m','live',NULL)")
            conn.execute("INSERT INTO forecast_emos_daily_high VALUES ('2025-06-01',1,99.0,9.0,8,'2025-06-02T00:00','m','rolling_origin',72)")

        adapter = SfoForecasterAdapter(root=root)

        assert adapter.load_emos_mu_sigma() == {date(2025, 6, 1): (71.0, 2.0)}
        assert adapter.load_emos_mu_sigma(source="rolling_origin") == {date(2025, 6, 1): (99.0, 9.0)}


def test_load_emos_mu_sigma_prefers_v2_per_lead_without_mixing_and_live_still_wins():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with sqlite3.connect(root / "weather.db") as conn:
            conn.execute(
                "CREATE TABLE forecast_emos_daily_high "
                "(target_date TEXT, lead_days INTEGER, predicted_high_f REAL, sigma_f REAL, "
                " n_models INTEGER, fetched_at TEXT, method TEXT, source TEXT, actual_high_f REAL, "
                " PRIMARY KEY (target_date, lead_days, source))"
            )
            conn.execute("INSERT INTO forecast_emos_daily_high VALUES ('2025-06-01',1,99,9,8,'z','m','rolling_origin',72)")
            conn.execute("INSERT INTO forecast_emos_daily_high VALUES ('2025-06-01',1,70,2,8,'a','m','rolling_origin_v2',72)")
            conn.execute("INSERT INTO forecast_emos_daily_high VALUES ('2025-06-02',2,73,3,8,'a','m','rolling_origin',74)")

        adapter = SfoForecasterAdapter(root=root)
        assert adapter.load_emos_mu_sigma(lead_days=1) == {
            date(2025, 6, 1): (70.0, 2.0)
        }
        assert adapter.load_emos_mu_sigma(lead_days=None) == {
            date(2025, 6, 1): (70.0, 2.0),
            date(2025, 6, 2): (73.0, 3.0),
        }
        assert adapter.load_emos_mu_sigma(source="rolling_origin") == {
            date(2025, 6, 1): (99.0, 9.0)
        }

        with sqlite3.connect(root / "weather.db") as conn:
            conn.execute("INSERT INTO forecast_emos_daily_high VALUES ('2025-06-01',1,71,2.5,8,'0','m','live',NULL)")
        assert adapter.load_emos_mu_sigma(lead_days=1) == {
            date(2025, 6, 1): (71.0, 2.5)
        }


def test_load_emos_mu_sigma_missing_db_or_table_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        adapter = SfoForecasterAdapter(root=root)
        assert adapter.load_emos_mu_sigma() == {}  # no weather.db at all
        sqlite3.connect(root / "weather.db").close()  # db exists, table does not
        assert adapter.load_emos_mu_sigma() == {}


def test_load_emos_outcomes_requires_final_cli_truth_when_finality_exists():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with sqlite3.connect(root / "weather.db") as conn:
            conn.execute(
                "CREATE TABLE forecast_emos_daily_high ("
                "station_id TEXT, target_date TEXT, lead_days INTEGER, "
                "predicted_high_f REAL, source TEXT, actual_high_f REAL)"
            )
            conn.execute(
                "INSERT INTO forecast_emos_daily_high VALUES "
                "('KSFO', '2026-07-10', 1, 70, 'rolling_origin', 68)"
            )
            conn.execute(
                "CREATE TABLE cli_settlements (station_id TEXT, local_date TEXT, "
                "max_temperature_f REAL, is_final INTEGER NOT NULL DEFAULT 1)"
            )
            conn.execute(
                "INSERT INTO cli_settlements VALUES ('KSFO', '2026-07-10', 71, 0)"
            )
        adapter = SfoForecasterAdapter(root=root)

        assert adapter.load_emos_outcomes() == []

        with sqlite3.connect(root / "weather.db") as conn:
            conn.execute("UPDATE cli_settlements SET is_final=1")
        outcomes = adapter.load_emos_outcomes()
        assert len(outcomes) == 1
        assert outcomes[0].actual_high_f == 71.0


def test_load_emos_outcomes_prefers_v2_without_mixing_legacy_calibration_rows():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with sqlite3.connect(root / "weather.db") as conn:
            conn.execute(
                "CREATE TABLE forecast_emos_daily_high ("
                "station_id TEXT, target_date TEXT, lead_days INTEGER, "
                "predicted_high_f REAL, source TEXT, actual_high_f REAL)"
            )
            conn.executemany(
                "INSERT INTO forecast_emos_daily_high VALUES (?, ?, 1, ?, ?, ?)",
                [
                    ("KSFO", "2026-07-09", 90, "rolling_origin", 70),
                    ("KSFO", "2026-07-10", 91, "rolling_origin", 71),
                    ("KSFO", "2026-07-10", 72, "rolling_origin_v2", 71),
                ],
            )
            conn.execute(
                "CREATE TABLE cli_settlements (station_id TEXT, local_date TEXT, "
                "max_temperature_f REAL, is_final INTEGER NOT NULL DEFAULT 1)"
            )
            conn.executemany(
                "INSERT INTO cli_settlements VALUES (?, ?, ?, 1)",
                [("KSFO", "2026-07-09", 70), ("KSFO", "2026-07-10", 71)],
            )

        outcomes = SfoForecasterAdapter(root=root).load_emos_outcomes()

        assert [(row.local_date, row.predicted_high_f) for row in outcomes] == [
            (date(2026, 7, 10), 72.0)
        ]
