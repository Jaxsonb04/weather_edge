import sqlite3
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from sfo_kalshi_quant.forecast_scorecards import (
    build_forecast_scorecards,
    build_research_evaluation_report,
)
from sfo_kalshi_quant.research_candidates import GAUSSIAN_PIT_CANDIDATE_KEY
from sfo_kalshi_quant.research_evidence import PairedEvidenceReport, build_paired_evidence_report
from sfo_kalshi_quant.research_google_join import GOOGLE_JOIN_REASON_VINTAGE_MISMATCH, GoogleJoinSkip
from sfo_kalshi_quant.research_operate import EvaluationRun
from sfo_kalshi_quant.research_promotion import (
    NO_EFFECT,
    PREDICTED_EDGE_SCOPE_YES_SIDE_TAKER,
    REASON_INSUFFICIENT_DAYS,
    ChallengerDeclaration,
    PromotionDecision,
)
from sfo_kalshi_quant.research_walkforward import WalkForwardEvidence


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE forecast_emos_daily_high (
            station_id TEXT NOT NULL, target_date TEXT NOT NULL,
            lead_days INTEGER NOT NULL, predicted_high_f REAL NOT NULL,
            sigma_f REAL NOT NULL, n_models INTEGER, model_spread_f REAL,
            fetched_at TEXT NOT NULL, method TEXT NOT NULL,
            source TEXT NOT NULL, actual_high_f REAL,
            PRIMARY KEY (station_id, target_date, lead_days, source)
        );
        CREATE TABLE cli_settlements (
            station_id TEXT NOT NULL, local_date TEXT NOT NULL,
            max_temperature_f REAL NOT NULL, fetched_at TEXT,
            source TEXT, PRIMARY KEY (station_id, local_date)
        );
        """
    )


def test_scorecards_join_truth_by_station_and_date() -> None:
    with TemporaryDirectory() as tmp:
        db = Path(tmp) / "weather.db"
        with sqlite3.connect(db) as conn:
            _schema(conn)
            conn.executemany(
                "INSERT INTO cli_settlements VALUES (?, ?, ?, 't', 'cli')",
                [("KSFO", "2026-07-01", 68), ("KNYC", "2026-07-01", 88)],
            )
            conn.executemany(
                "INSERT INTO forecast_emos_daily_high VALUES (?, '2026-07-01', 1, ?, 2, 8, 4, 't', 'emos_ngr', 'rolling_origin', NULL)",
                [("KSFO", 68), ("KNYC", 88)],
            )

        payload = build_forecast_scorecards(db)

    assert payload["available"] is True
    cards = {(row["station_id"], row["lead_days"]): row for row in payload["scorecards"]}
    assert cards[("KSFO", 1)]["mae_f"] == 0.0
    assert cards[("KNYC", 1)]["mae_f"] == 0.0
    assert len(cards) == 2


def test_scorecards_publish_probabilistic_metrics_and_fail_closed_gates() -> None:
    with TemporaryDirectory() as tmp:
        db = Path(tmp) / "weather.db"
        with sqlite3.connect(db) as conn:
            _schema(conn)
            for day in range(1, 11):
                target = f"2026-06-{day:02d}"
                conn.execute(
                    "INSERT INTO cli_settlements VALUES ('KSFO', ?, ?, 't', 'cli')",
                    (target, 60 + day),
                )
                conn.execute(
                    "INSERT INTO forecast_emos_daily_high VALUES ('KSFO', ?, 0, ?, 2, 8, 4, 't', 'emos_ngr', 'rolling_origin', NULL)",
                    (target, 60 + day),
                )

        payload = build_forecast_scorecards(db)
    card = payload["scorecards"][0]

    assert card["cases"] == 10
    assert card["crps"] > 0
    assert card["log_score"] > 0
    assert card["pit_mean"] == 0.5
    assert card["interval_coverage"]["80"] == 1.0
    gates = {row["key"]: row for row in payload["challenger_gates"]}
    assert gates["minimum_crps_emos"]["promotion_eligible"] is False
    assert "nested" in " ".join(gates["minimum_crps_emos"]["block_reasons"]).lower()
    assert gates["time_series_emos"]["required_cases_per_city"] == 180
    assert gates["analog_ensemble"]["required_cases_per_city"] == 365
    assert gates["pooled_distributional"]["required_pooled_station_days"] == 5000
    assert set(payload["shadow_challengers"]) == {
        "matched_lead_emos",
        "partial_pooled_intraday",
    }
    assert all(
        challenger["active"] is False
        for challenger in payload["shadow_challengers"].values()
    )


def test_scorecards_build_partial_pooled_intraday_shadow_cases() -> None:
    with TemporaryDirectory() as tmp:
        db = Path(tmp) / "weather.db"
        with sqlite3.connect(db) as conn:
            _schema(conn)
            conn.execute(
                "CREATE TABLE nws_station_observations ("
                "station_id TEXT, local_date TEXT, observed_at TEXT, temp_f REAL)"
            )
            for day in range(1, 4):
                target = f"2026-07-{day:02d}"
                conn.execute(
                    "INSERT INTO cli_settlements VALUES ('KSFO', ?, 70, 't', 'cli')",
                    (target,),
                )
                conn.execute(
                    "INSERT INTO forecast_emos_daily_high VALUES "
                    "('KSFO', ?, 0, 68, 1.5, 8, 4, 't', 'emos_ngr', "
                    "'rolling_origin_v2', NULL)",
                    (target,),
                )
                conn.execute(
                    "INSERT INTO nws_station_observations VALUES "
                    "('KSFO', ?, ?, 65)",
                    (target, f"{target}T20:00:00+00:00"),
                )

        payload = build_forecast_scorecards(db)

    challenger = payload["shadow_challengers"]["partial_pooled_intraday"]
    assert challenger["available"] is True
    assert challenger["cases"] == 3
    assert challenger["active"] is False


def test_scorecards_do_not_use_embedded_non_authoritative_actuals() -> None:
    with TemporaryDirectory() as tmp:
        db = Path(tmp) / "weather.db"
        with sqlite3.connect(db) as conn:
            _schema(conn)
            conn.execute(
                "INSERT INTO forecast_emos_daily_high VALUES ('KSFO', '2026-07-01', 1, 70, 2, 8, 4, 't', 'emos_ngr', 'rolling_origin', 70)"
            )

        payload = build_forecast_scorecards(db)

    assert payload["available"] is False
    assert payload["matched_cases"] == 0


def test_scorecards_exclude_preliminary_settlement_rows() -> None:
    with TemporaryDirectory() as tmp:
        db = Path(tmp) / "weather.db"
        with sqlite3.connect(db) as conn:
            _schema(conn)
            conn.execute(
                "ALTER TABLE cli_settlements "
                "ADD COLUMN is_final INTEGER NOT NULL DEFAULT 1"
            )
            conn.executemany(
                "INSERT INTO cli_settlements VALUES (?, ?, ?, 't', 'cli', ?)",
                [
                    ("KSFO", "2026-07-01", 68, 1),
                    ("KSFO", "2026-07-02", 71, 0),
                ],
            )
            conn.executemany(
                "INSERT INTO forecast_emos_daily_high VALUES "
                "('KSFO', ?, 1, ?, 2, 8, 4, 't', 'emos_ngr', 'rolling_origin', NULL)",
                [("2026-07-01", 68), ("2026-07-02", 71)],
            )

        payload = build_forecast_scorecards(db)

    assert payload["matched_cases"] == 1


def test_scorecards_prefer_v2_per_city_lead_method_without_double_counting() -> None:
    with TemporaryDirectory() as tmp:
        db = Path(tmp) / "weather.db"
        with sqlite3.connect(db) as conn:
            _schema(conn)
            conn.executemany(
                "INSERT INTO cli_settlements VALUES (?, ?, ?, 't', 'cli')",
                [
                    ("KSFO", "2026-07-01", 68),
                    ("KSFO", "2026-07-02", 69),
                    ("KNYC", "2026-07-01", 88),
                ],
            )
            conn.executemany(
                "INSERT INTO forecast_emos_daily_high VALUES (?, ?, 1, ?, 2, 8, 4, 't', 'emos_ngr', ?, NULL)",
                [
                    ("KSFO", "2026-07-01", 80, "rolling_origin"),
                    ("KSFO", "2026-07-02", 81, "rolling_origin"),
                    ("KSFO", "2026-07-02", 69, "rolling_origin_v2"),
                    ("KNYC", "2026-07-01", 88, "rolling_origin"),
                ],
            )

        payload = build_forecast_scorecards(db)

    assert payload["matched_cases"] == 2
    cards = {(row["station_id"], row["source"]): row for row in payload["scorecards"]}
    assert cards[("KSFO", "rolling_origin_v2")]["cases"] == 1
    assert cards[("KSFO", "rolling_origin_v2")]["mae_f"] == 0.0
    assert cards[("KNYC", "rolling_origin")]["cases"] == 1
    assert not any(station == "KSFO" and source == "rolling_origin" for station, source in cards)


def test_scorecards_do_not_fall_back_when_scope_has_unscored_v2_rows() -> None:
    with TemporaryDirectory() as tmp:
        db = Path(tmp) / "weather.db"
        with sqlite3.connect(db) as conn:
            _schema(conn)
            conn.execute(
                "INSERT INTO cli_settlements VALUES ('KSFO', '2026-07-01', 68, 't', 'cli')"
            )
            conn.execute(
                "INSERT INTO forecast_emos_daily_high VALUES "
                "('KSFO', '2026-07-01', 1, 80, 2, 8, 4, 't', 'emos_ngr', "
                "'rolling_origin', NULL)"
            )
            # The v2 rebuild has begun for this exact city/lead/method scope,
            # but its target is not settled yet. Reusing v1 would silently
            # report a different model version while labelling v2 as current.
            conn.execute(
                "INSERT INTO forecast_emos_daily_high VALUES "
                "('KSFO', '2026-07-02', 1, 69, 2, 8, 4, 't', 'emos_ngr', "
                "'rolling_origin_v2', NULL)"
            )

        payload = build_forecast_scorecards(db)

    assert payload["available"] is False
    assert payload["matched_cases"] == 0


# ---------------------------------------------------------------------------
# Task 7: build_research_evaluation_report -- publish fold coverage, replay
# completeness, paired statistics, adjusted promotion gates, candidate
# version, immutable experiment identity, and the observed-not-guaranteed
# $50/day KPI language (plan Task 7 Step 3).
# ---------------------------------------------------------------------------


def _declaration() -> ChallengerDeclaration:
    return ChallengerDeclaration(
        experiment_id="exp-1",
        hypothesis_family="gaussian-pit-station-lead",
        candidate_key=GAUSSIAN_PIT_CANDIDATE_KEY,
        candidate_version="v1",
        evidence_role="confirmatory",
        predicted_edge_scope=PREDICTED_EDGE_SCOPE_YES_SIDE_TAKER,
        max_drawdown_tolerance_pct=0.10,
        crps_regression_tolerance=0.5,
        brier_regression_tolerance=0.5,
        calibration_gap_regression_tolerance=0.3,
    )


def _empty_walk_forward_evidence() -> WalkForwardEvidence:
    return WalkForwardEvidence(folds=(), unavailable=(), skips=())


def _empty_paired_report() -> PairedEvidenceReport:
    return build_paired_evidence_report(
        (), (), challenger_candidate_key=GAUSSIAN_PIT_CANDIDATE_KEY
    )


def _blocked_decision() -> PromotionDecision:
    return PromotionDecision(
        experiment_id="exp-1",
        eligible_for_target_paper=False,
        block_reasons=(REASON_INSUFFICIENT_DAYS,),
        effect_classification=NO_EFFECT,
        independent_confirmatory_days=0,
    )


def _evaluation_run(**overrides) -> EvaluationRun:
    defaults = dict(
        declaration=_declaration(),
        walk_forward=_empty_walk_forward_evidence(),
        report=_empty_paired_report(),
        decision=_blocked_decision(),
        persisted_fold_count=0,
        skipped_fold_persist_reasons=(),
        google_join_matched_row_count=0,
        google_join_skips=(),
        prior_family_attempts=(),
    )
    defaults.update(overrides)
    return EvaluationRun(**defaults)


def test_report_publishes_immutable_experiment_identity_and_candidate_version() -> None:
    report = build_research_evaluation_report(_evaluation_run())
    identity = report["experiment_identity"]
    assert identity["experiment_id"] == "exp-1"
    assert identity["hypothesis_family"] == "gaussian-pit-station-lead"
    assert identity["candidate_key"] == GAUSSIAN_PIT_CANDIDATE_KEY
    assert identity["candidate_version"] == "v1"


def test_report_publishes_fold_coverage_and_replay_completeness() -> None:
    report = build_research_evaluation_report(_evaluation_run())
    assert report["fold_coverage"] == {
        "folds": 0, "unavailable_folds": 0, "skipped_historical_rows": 0,
    }
    assert report["replay_completeness"]["paired_case_count"] == 0


def test_report_publishes_adjusted_promotion_gate_verdict() -> None:
    report = build_research_evaluation_report(_evaluation_run())
    gate = report["promotion_gate"]
    assert gate["eligible_for_target_paper"] is False
    assert REASON_INSUFFICIENT_DAYS in gate["block_reasons"]
    assert gate["live_activation_allowed"] is False


def test_report_never_labels_the_50_dollar_target_as_guaranteed() -> None:
    report = build_research_evaluation_report(_evaluation_run())
    label = report["daily_target_kpi"]["label"]
    assert "not" in label.lower() and "guarantee" in label.lower()
    assert "observed" in label.lower()


def test_report_shows_observed_hit_rate_and_shortfall_when_data_exists() -> None:
    records, exclusions = (), ()
    report_evidence = build_paired_evidence_report(
        records, exclusions, challenger_candidate_key=GAUSSIAN_PIT_CANDIDATE_KEY,
        start_day=date(2026, 1, 1), end_day=date(2026, 1, 3),
    )
    run = _evaluation_run(report=report_evidence)
    report = build_research_evaluation_report(run)
    kpi = report["daily_target_kpi"]
    assert kpi["observed_days"] == 3
    assert kpi["observed_hit_rate"] == 0.0
    assert kpi["observed_shortfall_vs_target"] == report_evidence.target_pnl


def test_report_publishes_google_evidence_join_diagnostics() -> None:
    run = _evaluation_run(
        google_join_matched_row_count=4,
        google_join_skips=(
            GoogleJoinSkip(
                source_context_hash="h1", station_id="KSFO",
                target_date="2026-01-01", reason=GOOGLE_JOIN_REASON_VINTAGE_MISMATCH,
            ),
        ),
    )
    report = build_research_evaluation_report(run)
    assert report["google_evidence_join"] == {"matched_row_count": 4, "vintage_mismatch_count": 1}


def test_report_publishes_persistence_diagnostics() -> None:
    run = _evaluation_run(
        persisted_fold_count=5, skipped_fold_persist_reasons=(("KSFO:2026-01-01", "declared after window"),)
    )
    report = build_research_evaluation_report(run)
    assert report["persistence"]["persisted_fold_count"] == 5
    assert report["persistence"]["skipped_fold_persist_reasons"] == [
        ("KSFO:2026-01-01", "declared after window")
    ]


def test_report_is_json_serializable() -> None:
    import json

    report = build_research_evaluation_report(_evaluation_run())
    json.dumps(report, default=str)  # must not raise
