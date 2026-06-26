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


def test_load_emos_mu_sigma_missing_db_or_table_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        adapter = SfoForecasterAdapter(root=root)
        assert adapter.load_emos_mu_sigma() == {}  # no weather.db at all
        sqlite3.connect(root / "weather.db").close()  # db exists, table does not
        assert adapter.load_emos_mu_sigma() == {}
