from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FORECASTER = ROOT / "forecaster"
if str(FORECASTER) not in sys.path:
    sys.path.insert(0, str(FORECASTER))

import forecast_backtest as fb
import google_weather_cache
from google_weather_cache import BLEND_WEIGHTS


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def _row(
    target_date: str,
    *,
    actual: float,
    google: float | None = None,
    nws: float | None = None,
    open_meteo: float | None = None,
    history: float | None = None,
    station_adj: float = 0.0,
    predicted: float | None = None,
    raw: float | None = None,
    lead: float = 20.0,
) -> fb.BlendRow:
    return fb.BlendRow(
        target_date=target_date,
        fetched_at=f"{target_date}T00:30:00+00:00",
        predicted_high_f=predicted if predicted is not None else (raw if raw is not None else actual),
        raw_weighted_prediction_f=raw,
        google_high_f=google,
        nws_high_f=nws,
        open_meteo_high_f=open_meteo,
        history_high_f=history,
        station_adjustment_f=station_adj,
        lead_hours=lead,
        actual_high_f=actual,
        truth_source="clisfo",
        details={},
    )


def _cohort_block(mae_by_cohort: dict[str, float], days: int = 20) -> dict:
    return {
        cohort: {"days": days, "mae": mae_by_cohort.get(cohort), "bias": 0.0, "within3": 90.0}
        for cohort in fb.COHORTS
    }


def _calibration_block(brier_by_cohort: dict[str, float]) -> dict:
    return {
        "overall": {"brier": 0.2, "brier_skill": 0.3},
        "by_settled_cohort": {
            cohort: {"days": 20, "brier": brier_by_cohort.get(cohort), "brier_skill": 0.1}
            for cohort in fb.COHORTS
        },
    }


def _result(*, mae: float, bias: float, cohort_maes: dict[str, float], briers: dict[str, float]) -> dict:
    return {
        "headline": {"days": 40, "mae": mae, "bias": bias},
        "by_settled_cohort": _cohort_block(cohort_maes),
        "calibration": _calibration_block(briers),
    }


# --------------------------------------------------------------------------- #
# Predictor kernel
# --------------------------------------------------------------------------- #


def test_production_predictor_reproduces_weighted_blend_plus_station_adjustment():
    # All four sources agree at 70F with a +1F station nudge -> blend is 71F.
    row = _row("2026-05-10", actual=70, google=70, nws=70, open_meteo=70, history=70, station_adj=1.0)
    predictor = fb.make_weighted_predictor(BLEND_WEIGHTS)
    assert abs(predictor(row, []) - 71.0) < 1e-9


def test_production_predictor_renormalizes_over_available_sources():
    # Only google + nws present: weights renormalize to 0.38/0.36.
    row = _row("2026-05-10", actual=70, google=70, nws=72)
    expected = (70 * 0.38 + 72 * 0.36) / (0.38 + 0.36)
    predictor = fb.make_weighted_predictor(BLEND_WEIGHTS)
    assert abs(predictor(row, []) - expected) < 1e-9


# --------------------------------------------------------------------------- #
# Rolling de-bias predictor
# --------------------------------------------------------------------------- #


def test_debias_returns_base_below_minimum_history():
    weights = {"google": 1.0}
    predictor = fb.make_debias_predictor(weights, min_history_days=30)
    history = [_row(f"2026-04-{d:02d}", actual=75, google=65) for d in range(1, 21)]  # 20 days
    new_row = _row("2026-05-01", actual=70, google=66)
    assert abs(predictor(new_row, history) - 66.0) < 1e-9  # no correction yet


def test_debias_correction_is_capped():
    weights = {"google": 1.0}
    predictor = fb.make_debias_predictor(weights, min_history_days=30, cap=1.5)
    # 40 prior days with a consistent +10F under-prediction; correction caps at +1.5.
    history = [_row(f"2026-04-{d:02d}", actual=75, google=65) for d in range(1, 41)]
    new_row = _row("2026-06-01", actual=70, google=66)
    assert abs(predictor(new_row, history) - 67.5) < 1e-9  # 66 + capped 1.5


def test_source_mos_predictor_learns_capped_per_source_bias_from_prior_days():
    weights = {"google": 0.5, "nws": 0.5}
    predictor = fb.make_source_mos_predictor(weights, min_history_days=30, cap=1.5)
    history = [
        _row(f"2026-04-{d:02d}", actual=72, google=70, nws=70)
        for d in range(1, 36)
    ]
    new_row = _row("2026-06-01", actual=70, google=66, nws=68)

    # Raw weighted blend is 67.0; both sources have a learned +2F residual, but
    # the live MOS-style correction is capped at +1.5F.
    assert abs(predictor(new_row, history) - 68.5) < 1e-9


def test_source_mos_predictor_returns_raw_blend_below_minimum_history():
    weights = {"google": 1.0}
    predictor = fb.make_source_mos_predictor(weights, min_history_days=30, cap=1.5)
    history = [_row(f"2026-04-{d:02d}", actual=75, google=65) for d in range(1, 20)]
    new_row = _row("2026-06-01", actual=70, google=66)
    assert abs(predictor(new_row, history) - 66.0) < 1e-9


def test_production_debias_holdout_guard_flags_tail_cohort_regression():
    # The live activation path (google_weather_cache) must refuse a correction
    # that worsens a cohort's holdout MAE -- zero tolerance on the warm/hot tail.
    holdout = (
        [{"cohort": "warm", "raw_pred": 72.0, "actual": 72.0} for _ in range(5)]
        + [{"cohort": "normal", "raw_pred": 65.0, "actual": 67.0} for _ in range(5)]
        + [{"cohort": "cold", "raw_pred": 55.0, "actual": 58.0} for _ in range(2)]
    )
    corrections = {"warm": 1.5, "normal": 1.5, "cold": 1.0}
    regressions, inconclusive = google_weather_cache._bias_holdout_cohort_regressions(
        holdout, corrections, 0.5
    )
    assert "warm" in regressions       # +1.5 on a perfect cohort regresses it
    assert "normal" not in regressions  # +1.5 improves an under-prediction
    assert "cold" in inconclusive       # too few holdout samples to judge


# --------------------------------------------------------------------------- #
# End-to-end backtest + acceptance
# --------------------------------------------------------------------------- #


def _perfect_google_rows(n: int = 40) -> list[fb.BlendRow]:
    rows = []
    for i in range(n):
        actual = 62 + (i % 8)  # 62..69, all normal cohort
        rows.append(
            _row(
                f"2026-{3 + i // 28:02d}-{1 + i % 28:02d}",
                actual=actual,
                google=actual,          # perfect
                nws=actual + 4,
                open_meteo=actual + 4,
                history=actual + 4,
            )
        )
    return rows


def test_perfect_source_candidate_beats_production_and_is_accepted():
    rows = _perfect_google_rows()
    production = fb.run_forecast_backtest(rows, fb.make_weighted_predictor(BLEND_WEIGHTS), label="prod")
    candidate = fb.run_forecast_backtest(rows, fb.make_weighted_predictor({"google": 1.0}), label="cand")
    paired = fb.compare_forecasters(production, candidate, samples=500, seed=1)

    assert candidate["headline"]["mae"] < production["headline"]["mae"]
    assert paired["ci_high"] < 0  # candidate strictly better
    assert paired["dm_p_value"] < 0.05

    verdict = fb.evaluate_acceptance(production, candidate, paired)
    assert verdict["accepted"]


def test_acceptance_rejects_aggregate_win_that_regresses_warm_tail():
    # Candidate wins overall but the warm tail gets worse: must be rejected.
    production = _result(
        mae=3.0, bias=0.0,
        cohort_maes={"cold": 2.0, "normal": 3.0, "warm": 3.0, "hot": 4.0},
        briers={"cold": 0.1, "normal": 0.3, "warm": 0.4, "hot": 0.5},
    )
    candidate = _result(
        mae=2.7, bias=0.0,
        cohort_maes={"cold": 2.0, "normal": 2.5, "warm": 3.6, "hot": 4.0},  # warm +0.6
        briers={"cold": 0.1, "normal": 0.3, "warm": 0.4, "hot": 0.5},
    )
    paired = {"ci_high": -0.1, "dm_p_value": 0.001, "dm_stat": -3.0, "mean_delta_f": -0.3}

    verdict = fb.evaluate_acceptance(production, candidate, paired)
    assert not verdict["accepted"]
    # Aggregate skill passes; only the hard tail gate blocks it.
    skill_check = next(c for c in verdict["checks"] if c["name"] == "aggregate_skill")
    assert skill_check["passed"]
    cohort_check = next(c for c in verdict["checks"] if c["name"] == "no_cohort_regression")
    assert not cohort_check["passed"]


def test_acceptance_rejects_when_ci_straddles_zero():
    # A point MAE win whose CI includes zero is not accepted (noise guard).
    base_cohorts = {"cold": 2.0, "normal": 3.0, "warm": 3.0, "hot": 4.0}
    briers = {"cold": 0.1, "normal": 0.3, "warm": 0.4, "hot": 0.5}
    production = _result(mae=3.0, bias=0.0, cohort_maes=base_cohorts, briers=briers)
    candidate = _result(mae=2.95, bias=0.0, cohort_maes=base_cohorts, briers=briers)
    # Point MAE win, but the CI upper bound is above 0 and DM is not significant.
    paired = {"ci_high": 0.05, "dm_p_value": 0.2, "dm_stat": -1.0, "mean_delta_f": -0.05}

    verdict = fb.evaluate_acceptance(production, candidate, paired)
    assert not verdict["accepted"]
    skill_check = next(c for c in verdict["checks"] if c["name"] == "aggregate_skill")
    assert not skill_check["passed"]


def test_acceptance_blocks_when_a_real_tail_regime_is_untestable_in_candidate():
    # Production has a real hot regime; candidate scored no hot days -> block
    # (the tail gate must never be silently bypassed).
    production = _result(
        mae=3.0, bias=0.0,
        cohort_maes={"cold": 2.0, "normal": 3.0, "warm": 3.0, "hot": 4.0},
        briers={"cold": 0.1, "normal": 0.3, "warm": 0.4, "hot": 0.5},
    )
    production["by_settled_cohort"]["hot"] = {"days": 12, "mae": 4.0, "bias": 0.0, "within3": 80.0}
    candidate = _result(
        mae=2.6, bias=0.0,
        cohort_maes={"cold": 2.0, "normal": 2.5, "warm": 3.0, "hot": None},
        briers={"cold": 0.1, "normal": 0.3, "warm": 0.4, "hot": 0.5},
    )
    candidate["by_settled_cohort"]["hot"] = {"days": 0, "mae": None, "bias": None, "within3": None}
    paired = {"ci_high": -0.2, "dm_p_value": 0.001, "dm_stat": -3.0, "mean_delta_f": -0.4}

    verdict = fb.evaluate_acceptance(production, candidate, paired)
    assert not verdict["accepted"]
    cohort_check = next(c for c in verdict["checks"] if c["name"] == "no_cohort_regression")
    assert not cohort_check["passed"]
    assert "hot" in cohort_check["detail"]


def test_acceptance_surfaces_thin_tail_as_inconclusive_without_blocking_forever():
    # Warm/hot too thin to judge in BOTH arms: surfaced as inconclusive (not
    # silently skipped) but does not block, so improvements are not frozen until
    # a full tail season accumulates.
    base = {"cold": 2.0, "normal": 3.0, "warm": 3.0, "hot": 4.0}
    briers = {"cold": 0.1, "normal": 0.3, "warm": 0.4, "hot": 0.5}
    production = _result(mae=3.0, bias=0.0, cohort_maes=base, briers=briers)
    candidate = _result(
        mae=2.7, bias=0.0,
        cohort_maes={"cold": 2.0, "normal": 2.5, "warm": 5.0, "hot": 6.0}, briers=briers,
    )
    for arm in (production, candidate):
        arm["by_settled_cohort"]["warm"]["days"] = 4
        arm["by_settled_cohort"]["hot"]["days"] = 3
    paired = {"ci_high": -0.2, "dm_p_value": 0.001, "dm_stat": -3.0, "mean_delta_f": -0.3}

    verdict = fb.evaluate_acceptance(production, candidate, paired)
    cohort_check = next(c for c in verdict["checks"] if c["name"] == "no_cohort_regression")
    assert cohort_check["passed"]
    assert "inconclusive" in cohort_check["detail"]
    assert verdict["accepted"]


# --------------------------------------------------------------------------- #
# Truth consistency: the re-scoring trap + divergence monitor
# --------------------------------------------------------------------------- #


def test_late_clisfo_rescore_upgrades_fallback_row():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "weather.db"
        with sqlite3.connect(db_path) as conn:
            google_weather_cache.init_archive(conn)
            # A row first scored against a non-CLISFO fallback (actual 70, pred 70).
            conn.execute(
                """
                INSERT INTO forecast_blend_daily_high (
                    fetched_at, target_date, method, predicted_high_f,
                    actual_high_f, abs_error_f, scored_at, truth_source
                ) VALUES (?, ?, 'test', ?, ?, ?, ?, ?)
                """,
                (
                    "2026-05-03T07:30:00+00:00",
                    "2026-05-04",
                    70.0,
                    70.0,
                    0.0,
                    "2026-05-04T20:00:00+00:00",
                    "nws_daily",
                ),
            )
            # CLISFO posts a day late with a different settlement (72F).
            conn.execute(
                "INSERT INTO cli_settlements (station_id, local_date, max_temperature_f, fetched_at) "
                "VALUES ('KSFO', ?, ?, ?)",
                ("2026-05-04", 72, "2026-05-05T18:00:00+00:00"),
            )
            google_weather_cache.update_scores_for_table(conn, "forecast_blend_daily_high")
            row = conn.execute(
                "SELECT actual_high_f, abs_error_f, truth_source FROM forecast_blend_daily_high"
            ).fetchone()

        assert row[0] == 72.0           # upgraded to CLISFO settlement
        assert row[1] == 2.0            # |70 - 72|
        assert row[2] == "clisfo"       # truth source flipped


def test_clisfo_rescore_is_idempotent_once_on_clisfo():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "weather.db"
        with sqlite3.connect(db_path) as conn:
            google_weather_cache.init_archive(conn)
            conn.execute(
                """
                INSERT INTO forecast_blend_daily_high (
                    fetched_at, target_date, method, predicted_high_f,
                    actual_high_f, abs_error_f, scored_at, truth_source
                ) VALUES (?, ?, 'test', 71.0, 72.0, 1.0, ?, 'clisfo')
                """,
                ("2026-05-03T07:30:00+00:00", "2026-05-04", "2026-05-05T18:00:00+00:00"),
            )
            conn.execute(
                "INSERT INTO cli_settlements (station_id, local_date, max_temperature_f, fetched_at) "
                "VALUES ('KSFO', ?, ?, ?)",
                ("2026-05-04", 72, "2026-05-05T18:00:00+00:00"),
            )
            rescored = google_weather_cache.update_scores_for_table(
                conn, "forecast_blend_daily_high"
            )
        assert rescored == 0  # already CLISFO; nothing to do


def test_clisfo_nws_divergence_is_reported_not_averaged():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "weather.db"
        with sqlite3.connect(db_path) as conn:
            google_weather_cache.init_archive(conn)
            conn.execute(
                """
                CREATE TABLE nws_daily_high_ground_truth (
                    station_id TEXT, local_date TEXT, high_f REAL, is_complete INTEGER
                )
                """
            )
            data = [("2026-05-01", 70, 70.4), ("2026-05-02", 73, 72.1), ("2026-05-03", 68, 68.0)]
            for local_date, clisfo, nws in data:
                conn.execute(
                    "INSERT INTO cli_settlements (station_id, local_date, max_temperature_f, fetched_at) "
                    "VALUES ('KSFO', ?, ?, '2026-05-04T00:00:00+00:00')",
                    (local_date, clisfo),
                )
                conn.execute(
                    "INSERT INTO nws_daily_high_ground_truth VALUES ('KSFO', ?, ?, 1)",
                    (local_date, nws),
                )
            report = fb.clisfo_nws_divergence(conn)

        assert report["available"]
        assert report["summary"]["days"] == 3
        # 2026-05-02: CLISFO 73 vs NWS round(72.1)=72 -> a bin flip.
        assert report["summary"]["bin_flips"] == 1
