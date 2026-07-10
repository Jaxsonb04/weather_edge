"""Network-free tests for the serve-time trailing EMOS recalibration."""

from __future__ import annotations

import math
import sqlite3
from datetime import date, timedelta

from emos_recalibration import (
    Correction,
    compute_correction,
    correction_for_serve,
    load_scored_series,
    window_rows,
)


def _rows(bias: float, sigma: float, n: int, *, truth: float = 70.0):
    """n window rows where the forecast ran exactly ``bias`` warm."""

    return [(truth + bias, sigma, truth) for _ in range(n)]


def test_empty_window_is_exact_noop():
    correction = compute_correction([])
    assert correction == Correction()
    assert correction.apply(71.5, 2.5) == (71.5, 2.5)


def test_disabled_toggles_are_exact_noop():
    rows = _rows(2.0, 2.0, 45)
    correction = compute_correction(rows, apply_bias=False, apply_sigma=False)
    assert correction.apply(71.5, 2.5) == (71.5, 2.5)


def test_bias_correction_shrinks_by_n_over_n_plus_k():
    # A constant +2F warm error has zero spread, so the significance deadband
    # subtracts nothing and the shrinkage factor is exactly n/(n+k).
    rows = _rows(2.0, 2.0, 45)
    correction = compute_correction(rows, k=10.0, apply_sigma=False)
    assert math.isclose(correction.bias_f, 2.0 * 45 / 55)
    mu, sigma = correction.apply(70.0, 2.0)
    assert math.isclose(mu, 70.0 - 2.0 * 45 / 55)
    assert sigma == 2.0  # sigma untouched when apply_sigma is off


def test_bias_correction_sign_symmetry():
    warm = compute_correction(_rows(1.5, 2.0, 45), apply_sigma=False)
    cold = compute_correction(_rows(-1.5, 2.0, 45), apply_sigma=False)
    assert math.isclose(warm.bias_f, -cold.bias_f)
    assert warm.bias_f > 0.0  # warm history -> mu lowered


def test_bias_deadband_zeroes_insignificant_bias():
    # Alternating +1/-1 errors around a tiny +0.05F mean: |mean| is far below
    # t * stderr, so the soft threshold must zero the correction entirely.
    rows = []
    for i in range(40):
        error = 1.05 if i % 2 == 0 else -0.95
        rows.append((70.0 + error, 2.0, 70.0))
    correction = compute_correction(rows, apply_sigma=False, bias_deadband_t=1.5)
    assert correction.bias_f == 0.0


def test_bias_deadband_soft_thresholds_significant_bias():
    # Errors of +2 +/- 1 alternating: mean 2.0, stdev ~1.0126, n=40.
    rows = []
    for i in range(40):
        error = 3.0 if i % 2 == 0 else 1.0
        rows.append((70.0 + error, 2.0, 70.0))
    errors = [mu - truth for mu, _s, truth in rows]
    mean = sum(errors) / len(errors)
    variance = sum((e - mean) ** 2 for e in errors) / (len(errors) - 1)
    stderr = math.sqrt(variance) / math.sqrt(len(errors))
    correction = compute_correction(rows, k=10.0, apply_sigma=False, bias_deadband_t=1.5)
    expected = (40 / 50) * (mean - 1.5 * stderr)
    assert math.isclose(correction.bias_f, expected, rel_tol=1e-9)
    assert 0.0 < correction.bias_f < mean


def test_bias_needs_at_least_three_rows():
    correction = compute_correction(_rows(2.0, 2.0, 2), apply_sigma=False)
    assert correction.bias_f == 0.0


def test_sigma_factor_shrinks_toward_one_and_clips():
    # Residuals exactly +/- sigma -> raw factor 1.0 -> no-op even when on.
    rows = [(70.0 + (2.0 if i % 2 == 0 else -2.0), 2.0, 70.0) for i in range(40)]
    correction = compute_correction(rows, apply_bias=False)
    assert math.isclose(correction.sigma_factor, 1.0)

    # Massive dispersion clips at the ceiling; near-zero clips at the floor.
    wide = compute_correction(_rows(6.0, 1.0, 45), apply_bias=False)
    assert wide.sigma_factor == 1.5
    narrow = compute_correction(_rows(0.0, 10.0, 45), apply_bias=False)
    assert narrow.sigma_factor == 0.75


def test_sigma_dispersion_is_net_of_applied_bias():
    # A pure-bias window: once the bias correction is applied the corrected
    # forecast's residuals are tiny, so sigma should shrink (floor-clipped),
    # NOT inflate for the bias it no longer pays.
    rows = _rows(4.0, 2.0, 45)
    both = compute_correction(rows, k=10.0)
    raw = compute_correction(rows, k=10.0, sigma_net_of_bias=False)
    assert both.sigma_factor < raw.sigma_factor
    assert raw.sigma_factor == 1.5  # bias^2 inflates the raw variant to the cap


def test_degenerate_sigmas_leave_factor_at_one():
    rows = [(72.0, 0.0, 70.0)] * 10
    correction = compute_correction(rows, apply_bias=False)
    assert correction.sigma_factor == 1.0


def test_window_rows_excludes_serve_date_and_older_than_window():
    serve = date(2026, 7, 10)
    series = [
        (serve - timedelta(days=46), 70.0, 2.0, 70.0),  # too old
        (serve - timedelta(days=45), 71.0, 2.0, 70.0),  # oldest inside
        (serve - timedelta(days=1), 72.0, 2.0, 70.0),   # newest inside
        (serve, 73.0, 2.0, 70.0),                        # serve day itself: leak
        (serve + timedelta(days=1), 74.0, 2.0, 70.0),    # future: leak
    ]
    rows = window_rows(series, serve, window_days=45)
    assert [mu for mu, _s, _t in rows] == [71.0, 72.0]


def _seed_db(conn: sqlite3.Connection, *, bias: float, days: int, station: str = "KSEA"):
    conn.execute(
        """
        CREATE TABLE forecast_emos_daily_high (
            station_id TEXT, target_date TEXT, lead_days INTEGER,
            predicted_high_f REAL, sigma_f REAL, n_models INTEGER,
            model_spread_f REAL, fetched_at TEXT, method TEXT,
            source TEXT, actual_high_f REAL
        )
        """
    )
    conn.execute(
        "CREATE TABLE cli_settlements (station_id TEXT, local_date TEXT, "
        "max_temperature_f INTEGER, fetched_at TEXT, source TEXT)"
    )
    start = date(2026, 5, 1)
    for i in range(days):
        day = (start + timedelta(days=i)).isoformat()
        truth = 70 + (i % 5)
        conn.execute(
            "INSERT INTO forecast_emos_daily_high VALUES (?, ?, 1, ?, 2.0, 8, "
            "1.0, 'x', 'emos_wmean', 'rolling_origin', NULL)",
            (station, day, truth + bias),
        )
        conn.execute(
            "INSERT INTO cli_settlements VALUES (?, ?, ?, 'x', 'nws_cli')",
            (station, day, truth),
        )
    conn.commit()
    return start + timedelta(days=days)  # first unsettled day


def test_correction_for_serve_reads_scored_rolling_rows():
    conn = sqlite3.connect(":memory:")
    serve_date = _seed_db(conn, bias=2.0, days=45)
    correction = correction_for_serve(conn, "KSEA", 1, serve_date)
    assert correction.n_window == 45
    assert math.isclose(correction.bias_f, 2.0 * 45 / 55)

    # Truth joined from cli_settlements, not the archive's actual_high_f
    # column (deliberately NULL above).
    series = load_scored_series(conn, "KSEA", 1)
    assert len(series) == 45


def test_correction_for_serve_missing_tables_is_noop():
    conn = sqlite3.connect(":memory:")
    correction = correction_for_serve(conn, "KSEA", 1, date(2026, 7, 10))
    assert correction == Correction()


def test_correction_for_serve_ignores_other_station_and_lead():
    conn = sqlite3.connect(":memory:")
    serve_date = _seed_db(conn, bias=2.0, days=45, station="KSEA")
    assert correction_for_serve(conn, "KNYC", 1, serve_date) == Correction()
    assert correction_for_serve(conn, "KSEA", 2, serve_date) == Correction()
