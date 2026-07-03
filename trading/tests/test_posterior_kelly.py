"""Tests for posterior-mean Kelly sizing (Phase 2b)."""

import sqlite3

from pytest import approx

from sfo_kalshi_quant.posterior_kelly import (
    CohortRecord,
    PosteriorKellyModel,
    _accumulate,
    calibration_trust,
    load_posterior_kelly_model,
    posterior_win_rate,
    resolve_record,
)


def test_posterior_win_rate_sits_at_breakeven_with_no_record():
    empty = CohortRecord(n=0, wins=0.0, mean_claimed_prob=0.7, mean_cost=0.55)
    # No data -> the prior (centered on breakeven cost) fully determines it.
    assert posterior_win_rate(empty, prior_strength=20.0) == approx(0.55)


def test_posterior_win_rate_moves_toward_realized_with_data():
    # 100 trades, 80 wins, breakeven 0.55: posterior pulls toward 0.8 but is
    # shrunk by the 20-pseudo-count prior at 0.55.
    rec = CohortRecord(n=100, wins=80.0, mean_claimed_prob=0.75, mean_cost=0.55)
    expected = (80.0 + 20.0 * 0.55) / (100.0 + 20.0)
    assert posterior_win_rate(rec, prior_strength=20.0) == approx(expected)


def test_no_record_gives_zero_trust():
    empty = CohortRecord(n=0, wins=0.0, mean_claimed_prob=0.7, mean_cost=0.55)
    # Short record -> posterior at breakeven -> no supported edge -> trust 0.
    assert calibration_trust(empty, prior_strength=20.0) == 0.0


def test_calibrated_record_earns_near_full_trust():
    # Model claims 0.75, breakeven 0.55 (claimed edge 0.20). Realized 0.75 over a
    # large sample -> posterior ~0.72 -> realized edge ~0.17 -> trust ~0.85.
    rec = CohortRecord(n=400, wins=300.0, mean_claimed_prob=0.75, mean_cost=0.55)
    trust = calibration_trust(rec, prior_strength=20.0)
    assert trust > 0.8
    assert trust <= 1.0


def test_overconfident_record_shrinks_trust():
    # Model claims 0.75 but only wins 0.58 of the time: barely above breakeven ->
    # low trust despite a large sample.
    rec = CohortRecord(n=400, wins=232.0, mean_claimed_prob=0.75, mean_cost=0.55)
    assert calibration_trust(rec, prior_strength=20.0) < 0.25


def test_losing_record_gives_zero_trust():
    # Realized win-rate below breakeven -> realized edge negative -> clamped to 0.
    rec = CohortRecord(n=200, wins=90.0, mean_claimed_prob=0.70, mean_cost=0.55)
    assert calibration_trust(rec, prior_strength=20.0) == 0.0


def test_no_claimed_edge_gives_zero_trust():
    rec = CohortRecord(n=200, wins=180.0, mean_claimed_prob=0.55, mean_cost=0.55)
    assert calibration_trust(rec, prior_strength=20.0) == 0.0


def test_resolve_record_falls_back_to_overall_when_cohort_thin():
    cohort_records = {"hot_80f_plus": CohortRecord(2, 2.0, 0.7, 0.5)}
    overall = CohortRecord(100, 70.0, 0.7, 0.5)
    # Thin cohort (n=2 < 8) -> pooled record.
    assert resolve_record("hot_80f_plus", cohort_records, overall, min_cohort_n=8) is overall
    # Unknown cohort -> pooled record.
    assert resolve_record(None, cohort_records, overall, min_cohort_n=8) is overall
    # Rich cohort -> its own record.
    cohort_records["warm_70_79f"] = CohortRecord(20, 15.0, 0.7, 0.5)
    assert resolve_record("warm_70_79f", cohort_records, overall, min_cohort_n=8).n == 20


def test_size_multiplier_spans_floor_to_one():
    model = PosteriorKellyModel(
        cohort_records={
            "cool_le_69f": CohortRecord(400, 300.0, 0.75, 0.55),   # calibrated -> ~1
            "warm_70_79f": CohortRecord(200, 90.0, 0.70, 0.55),    # losing -> floor
        },
        overall=CohortRecord(100, 70.0, 0.70, 0.55),
        prior_strength=20.0,
        floor=0.2,
        min_cohort_n=8,
    )
    hot_mult = model.size_multiplier("cool_le_69f")
    warm_mult = model.size_multiplier("warm_70_79f")
    assert warm_mult == approx(0.2)          # losing cohort pinned at the floor
    assert hot_mult > 0.8                     # calibrated cohort sized near full
    assert 0.2 <= warm_mult <= hot_mult <= 1.0


def test_floor_zero_stands_down_a_losing_cohort_completely():
    model = PosteriorKellyModel(
        cohort_records={"warm_70_79f": CohortRecord(200, 90.0, 0.70, 0.55)},
        overall=CohortRecord(200, 90.0, 0.70, 0.55),
        prior_strength=20.0,
        floor=0.0,
        min_cohort_n=8,
    )
    assert model.size_multiplier("warm_70_79f") == 0.0


def test_accumulate_scores_wins_and_cohorts():
    # (claimed, cost, won, settlement_high_f)
    rows = [
        (0.90, 0.85, True, 72.0),   # warm win
        (0.90, 0.85, False, 74.0),  # warm loss
        (0.60, 0.50, True, 65.0),   # normal win
    ]
    cohort_records, overall = _accumulate(rows)
    assert overall.n == 3
    assert overall.wins == 2.0  # two of three won
    assert cohort_records["warm_70_79f"].n == 2
    assert cohort_records["warm_70_79f"].wins == 1.0
    assert cohort_records["normal_60_69f"].wins == 1.0  # 65F settles normal_60_69f


def _seed_orders(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    conn.execute(
        "CREATE TABLE paper_orders (side TEXT, probability REAL, cost_per_contract REAL, "
        "resolved_yes INTEGER, settlement_high_f REAL, settled_at TEXT, status TEXT, "
        "target_date TEXT, strike_type TEXT, floor_strike REAL, cap_strike REAL)"
    )
    conn.executemany(
        "INSERT INTO paper_orders (side, probability, cost_per_contract, resolved_yes, "
        "settlement_high_f, settled_at, status, target_date, strike_type, floor_strike, "
        "cap_strike) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()


def test_load_counts_settled_actual_outcomes():
    conn = sqlite3.connect(":memory:")
    _seed_orders(
        conn,
        [
            # side, prob, cost, resolved_yes, high, settled_at, status, date, stype, floor, cap
            ("NO", 0.92, 0.85, 0, 65.0, "t", "PAPER_SETTLED", "2026-06-15", "range", 64.0, 67.0),
            ("NO", 0.92, 0.85, 0, 66.0, "t", "PAPER_SETTLED", "2026-06-16", "range", 64.0, 67.0),
        ],
    )
    model = load_posterior_kelly_model(
        conn, include_counterfactual_closed=False, prior_strength=20.0, floor=0.2, min_cohort_n=8
    )
    assert model.overall.n == 2
    assert model.overall.wins == 2.0  # both NO settled winners
    assert 0.2 <= model.size_multiplier("normal_60_69f") <= 1.0


def test_counterfactual_closed_orders_are_scored_only_on_known_high_dates():
    conn = sqlite3.connect(":memory:")
    _seed_orders(
        conn,
        [
            # A settled NO winner establishes the authoritative high (66) for 6-15.
            ("NO", 0.92, 0.85, 0, 66.0, "t", "PAPER_SETTLED", "2026-06-15", "less", None, 70.0),
            # Closed YES on the 70-72 bin, same date: high 66 -> bin resolves NO -> YES loses.
            ("YES", 0.30, 0.28, None, None, None, "PAPER_CLOSED", "2026-06-15", "range", 70.0, 72.0),
            # Closed order on a date with NO authoritative high -> skipped (no obs-max proxy).
            ("YES", 0.30, 0.28, None, None, None, "PAPER_CLOSED", "2026-06-20", "range", 70.0, 72.0),
        ],
    )
    full = load_posterior_kelly_model(
        conn, include_counterfactual_closed=True, prior_strength=20.0, floor=0.2, min_cohort_n=8
    )
    # 1 settled win + 1 counterfactual loss; the unknown-high closed order is skipped.
    assert full.overall.n == 2
    assert full.overall.wins == 1.0
    # Disabling the counterfactual path leaves only the settled order.
    held_only = load_posterior_kelly_model(
        conn, include_counterfactual_closed=False, prior_strength=20.0, floor=0.2, min_cohort_n=8
    )
    assert held_only.overall.n == 1
    assert held_only.overall.wins == 1.0
    # Seeing the extra loss lowers the demonstrated win-rate -> a smaller multiplier.
    assert full.size_multiplier("normal_60_69f") <= held_only.size_multiplier("normal_60_69f")
