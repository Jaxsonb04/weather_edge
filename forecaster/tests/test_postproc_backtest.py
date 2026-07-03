"""Network-free tests for the probabilistic scoreboard (Phase 0)."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from forecast_postproc_backtest import (
    MIN_CONSENSUS_MODELS,
    DayScore,
    PredictorScore,
    brier_by_cohort,
    brier_with_shared_sigma,
    build_climatology,
    crps_gate,
    evaluate,
    gaussian_crps,
    make_nwp_consensus_predictor,
    score_predictor,
)
from google_weather_cache import predicted_temperature_cohort
from nwp_archive import ensure_schema, upsert_forecasts


def test_gaussian_crps_closed_form_constant():
    # At y == mu, CRPS(N(mu, sigma), mu) = sigma * (2*phi(0) - 1/sqrt(pi)) = 0.233692*sigma.
    crps = gaussian_crps(50.0, 2.0, 50.0)
    assert abs(crps - 2.0 * 0.2336916) < 1e-4


def test_gaussian_crps_increases_with_error():
    sharp_hit = gaussian_crps(50.0, 2.0, 50.0)
    big_miss = gaussian_crps(50.0, 2.0, 56.0)
    assert big_miss > sharp_hit


def test_gaussian_crps_rewards_honest_sigma():
    # With a fixed 5F miss, a sigma matched to the error beats both an
    # over-confident (too tight) and an under-confident (too wide) distribution.
    matched = gaussian_crps(70.0, 5.0, 75.0)
    too_tight = gaussian_crps(70.0, 1.5, 75.0)
    too_wide = gaussian_crps(70.0, 20.0, 75.0)
    assert matched < too_tight
    assert matched < too_wide


def test_crps_gate_flags_direction():
    truth = {f"2025-06-{i:02d}": 70.0 for i in range(1, 21)}
    good = lambda _d, _m: (70.0, 2.0)   # noqa: E731 - dead on
    bad = lambda _d, _m: (78.0, 2.0)    # noqa: E731 - 8F high every day
    good_score = score_predictor("good", good, truth)
    bad_score = score_predictor("bad", bad, truth)

    # candidate=good vs reference=bad -> good has lower CRPS -> negative, ci upper < 0
    gate = crps_gate(good_score, bad_score)
    assert gate["mean_delta"] < 0
    assert gate["ci"][1] < 0, "a clearly better predictor must clear the ship gate"

    # candidate=bad vs reference=good -> positive delta (worse)
    assert crps_gate(bad_score, good_score)["mean_delta"] > 0


def test_build_climatology_returns_seasonal_mu_sigma():
    base = date(2025, 6, 1)
    truth = {(base + timedelta(days=i)).isoformat(): 65.0 + (i % 5) for i in range(60)}
    clim = build_climatology(truth)
    doy = base.timetuple().tm_yday + 20  # mid-window, well-sampled
    assert doy in clim
    mu, sigma = clim[doy]
    assert 64.0 < mu < 70.0
    assert sigma >= 1.5


def _seed_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE clisfo_settlements "
        "(local_date TEXT PRIMARY KEY, max_temperature_f INTEGER, fetched_at TEXT, source TEXT)"
    )
    base = date(2025, 6, 1)
    for i in range(61):
        day = (base + timedelta(days=i)).isoformat()
        conn.execute(
            "INSERT INTO clisfo_settlements VALUES (?, ?, ?, ?)",
            (day, 65 + (i % 7), "x", "test"),
        )
    ensure_schema(conn)
    rows = []
    for i in range(10, 50):  # archive a mid subset so consensus has coverage
        day = (base + timedelta(days=i)).isoformat()
        truth = 65 + (i % 7)
        for model, offset in (("gfs_seamless", 0.5), ("ecmwf_ifs025", -0.5), ("ncep_nbm_conus", 0.0)):
            rows.append((day, model, 1, truth + offset, "x", "test"))
    upsert_forecasts(conn, rows)
    conn.commit()
    return conn


def test_evaluate_runs_end_to_end_and_consensus_beats_climatology():
    conn = _seed_db()
    result = evaluate(conn, lead_days=1, reference_name="climatology")

    consensus = result["scores"]["nwp_consensus"]
    climo = result["scores"]["climatology"]
    assert consensus.days > 0
    assert consensus.aggregate()["crps"] is not None

    # The archived models sit within +/-0.5F of truth by construction, so the
    # multi-model consensus must beat the seasonal-mean baseline on MAE.
    assert consensus.aggregate()["mae"] < climo.aggregate()["mae"]

    # Baseline blend has no archive table here -> zero overlap, handled gracefully.
    assert result["scores"]["baseline_blend"].days == 0


def test_gaussian_crps_far_miss_approaches_abs_error():
    # As the standardized miss grows, CRPS -> |y - mu|.
    assert abs(gaussian_crps(50.0, 1.5, 100.0) - 50.0) < 1.0


def test_consensus_floor_excludes_thin_days_and_uses_sample_sigma():
    nwp = {
        "2025-06-10": {"a": 70.0, "b": 71.0},               # 2 models < floor -> excluded
        "2025-06-11": {"a": 60.0, "b": 65.0, "c": 70.0},    # 3 models -> scored
    }
    assert MIN_CONSENSUS_MODELS == 3
    predict = make_nwp_consensus_predictor(nwp, fallback_sigma=3.0)
    assert predict("2025-06-10", None) is None
    assert predict("2099-01-01", None) is None  # no models
    mu, sigma = predict("2025-06-11", None)
    assert abs(mu - 65.0) < 1e-9
    assert abs(sigma - 5.0) < 1e-9  # sample (n-1) stdev of [60, 65, 70], not population


def test_climatology_wraps_year_boundary():
    truth = {
        "2023-12-26": 50.0, "2023-12-27": 51.0, "2023-12-28": 52.0, "2023-12-29": 53.0,
        "2023-12-30": 54.0, "2023-12-31": 55.0,
        "2024-01-01": 56.0, "2024-01-02": 57.0, "2024-01-03": 58.0, "2024-01-04": 59.0,
        "2024-01-05": 60.0,
    }
    clim = build_climatology(truth, window=15)
    jan1_doy = date(2024, 1, 1).timetuple().tm_yday
    assert jan1_doy in clim
    mu, _sigma = clim[jan1_doy]
    # With the cyclic window, Jan 1 must pool the late-December samples too, so mu
    # is the full mean (~55), not the Jan-only mean (~58). Proves the wrap fires.
    assert abs(mu - 55.0) < 1.0


def test_shared_sigma_brier_ranks_by_location_only():
    truth_days = [(f"2025-06-{i:02d}", 70.0) for i in range(1, 21)]  # 70F == 'warm'

    def make(bias: float, own_sigma: float) -> PredictorScore:
        score = PredictorScore(name="x")
        for date_str, truth in truth_days:
            mu = truth + bias
            score.per_day.append(
                DayScore(date_str, mu, own_sigma, truth, abs(mu - truth), mu - truth, 0.0,
                         predicted_temperature_cohort(truth))
            )
        return score

    shared = {"warm": 2.0, "overall": 2.0}
    centered = make(0.0, 99.0)   # accurate but absurd OWN sigma
    biased = make(5.0, 0.1)      # off by 5F with a tiny OWN sigma
    assert brier_with_shared_sigma(centered, shared) < brier_with_shared_sigma(biased, shared)
    # OWN sigma is ignored -> changing it does not move the shared-sigma Brier.
    assert abs(brier_with_shared_sigma(make(0.0, 1.0), shared)
               - brier_with_shared_sigma(centered, shared)) < 1e-12


def test_brier_sigma_fallback_chain_does_not_crash():
    score = PredictorScore(name="x")
    score.per_day.append(DayScore("2025-06-10", 70.0, 2.0, 70.0, 0.0, 0.0, 0.0, "warm"))
    assert brier_with_shared_sigma(score, {"overall": 2.0}) is not None  # cohort -> overall
    assert brier_with_shared_sigma(score, {}) is not None                # -> SIGMA_FLOOR_F


def test_brier_by_cohort_splits_and_matches_aggregate_within_cohort():
    # Two warm days (biased) and two hot days (accurate): the cohort split must
    # isolate the biased-warm penalty from the accurate-hot calibration, and a
    # cohort with no days must report None rather than crash.
    score = PredictorScore(name="x")
    for date_str in ("2025-06-01", "2025-06-02"):  # warm, off by 4F
        score.per_day.append(DayScore(date_str, 74.0, 2.0, 70.0, 4.0, 4.0, 0.0, "warm"))
    for date_str in ("2025-07-01", "2025-07-02"):  # hot, on target
        score.per_day.append(DayScore(date_str, 82.0, 2.0, 82.0, 0.0, 0.0, 0.0, "hot"))

    shared = {"warm": 2.0, "hot": 2.0, "overall": 2.0}
    by_cohort = brier_by_cohort(score, shared)
    # The biased-warm cohort must score worse (higher Brier) than accurate-hot.
    assert by_cohort["warm"] > by_cohort["hot"]
    # A cohort with no scored days is reported as None, not zero.
    assert by_cohort["cold"] is None
    # Restricting the aggregate to one cohort's days reproduces that cohort's cell.
    warm_only = PredictorScore(name="x", per_day=[d for d in score.per_day if d.settled_cohort == "warm"])
    assert abs(by_cohort["warm"] - brier_with_shared_sigma(warm_only, shared)) < 1e-12


def test_crps_gate_insufficient_overlap_returns_none():
    truth = {f"2025-06-0{i}": 70.0 for i in (1, 2)}  # only 2 shared days
    predict = lambda _d, _m: (70.0, 2.0)  # noqa: E731
    a = score_predictor("a", predict, truth)
    b = score_predictor("b", predict, truth)
    gate = crps_gate(a, b)
    assert gate["n"] < 3 and gate["mean_delta"] is None and gate["ci"] is None


def test_crps_gate_uses_only_overlapping_days():
    predict = lambda _d, _m: (70.0, 2.0)  # noqa: E731
    cand = score_predictor("c", predict, {f"2025-06-0{i}": 70.0 for i in (1, 2, 3)})
    ref = score_predictor("r", predict, {f"2025-06-0{i}": 70.0 for i in (2, 3, 4)})
    assert crps_gate(cand, ref)["n"] == 2  # overlap = {06-02, 06-03}


def test_evaluate_falls_back_to_climatology_when_reference_empty():
    conn = _seed_db()  # has clisfo + nwp but NO forecast_blend_daily_high table
    result = evaluate(conn, lead_days=1, reference_name="baseline_blend")
    assert result["reference"] == "climatology"           # silent reference swap fired
    assert "climatology" not in result["gates"]           # reference excluded from gates
    assert "baseline_blend" in result["gates"]


def test_evaluate_missing_nwp_table_does_not_raise():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE clisfo_settlements "
        "(local_date TEXT PRIMARY KEY, max_temperature_f INTEGER, fetched_at TEXT, source TEXT)"
    )
    conn.execute("INSERT INTO clisfo_settlements VALUES ('2025-06-10', 70, 'x', 't')")
    # no nwp_model_forecasts and no forecast_blend_daily_high tables
    result = evaluate(conn, lead_days=1, reference_name="climatology")
    assert result["nwp_days"] == 0
    assert result["scores"]["nwp_consensus"].days == 0
