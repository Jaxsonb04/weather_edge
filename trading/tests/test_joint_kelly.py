"""Tests for the joint Kelly allocator across mutually exclusive bins (Phase 2c)."""

import math

from pytest import approx

from sfo_kalshi_quant.joint_kelly import (
    JointPosition,
    build_joint_positions,
    expected_log_growth,
    joint_kelly_fractions,
)


def test_single_bet_matches_scalar_kelly():
    # One YES position, cost 0.5 (even money, win_return 1.0), true win prob 0.6.
    # Scalar Kelly fraction f* = (bp - q)/b with b=1 -> 0.6 - 0.4 = 0.2.
    pos = [JointPosition("a", frozenset({1}), cost=0.5)]
    probs = {1: 0.6, 0: 0.4}
    fractions = joint_kelly_fractions(pos, probs, total_fraction_cap=1.0)
    assert fractions["a"] == approx(0.2, abs=0.02)


def test_no_edge_bet_is_not_taken():
    # Even-money bet at the true probability -> zero edge -> zero fraction.
    pos = [JointPosition("a", frozenset({1}), cost=0.5)]
    probs = {1: 0.5, 0: 0.5}
    fractions = joint_kelly_fractions(pos, probs, total_fraction_cap=1.0)
    assert fractions["a"] == approx(0.0, abs=1e-3)


def test_negative_edge_bet_is_rejected():
    # Overpriced: cost 0.6 (implies 0.6) but true prob only 0.4 -> no stake.
    pos = [JointPosition("a", frozenset({1}), cost=0.6)]
    probs = {1: 0.4, 0: 0.6}
    fractions = joint_kelly_fractions(pos, probs, total_fraction_cap=1.0)
    assert fractions["a"] == approx(0.0, abs=1e-3)


def test_hedged_bins_both_get_stake():
    # Two mutually exclusive YES bins, each cheap relative to its true prob.
    # Bin 1 wins on scenario 1, bin 2 on scenario 2; jointly they hedge, so both
    # earn a positive stake.
    pos = [
        JointPosition("bin1", frozenset({1}), cost=0.40),
        JointPosition("bin2", frozenset({2}), cost=0.40),
    ]
    probs = {1: 0.5, 2: 0.5}
    fractions = joint_kelly_fractions(pos, probs, total_fraction_cap=1.0)
    assert fractions["bin1"] > 0.05
    assert fractions["bin2"] > 0.05


def test_joint_beats_isolated_growth_for_correlated_no_basket():
    # Three NO positions across a 3-bin ladder: each wins unless the high lands
    # in its own bin. Solve jointly, then confirm the joint allocation's expected
    # log growth is at least as high as sizing each NO in isolation at scalar
    # Kelly and stacking them.
    ladder = [1, 2, 3]
    probs = {1: 1 / 3, 2: 1 / 3, 3: 1 / 3}
    no_cost = 0.60  # NO on each bin costs 0.60 (implies the bin wins 0.40)
    positions = [
        JointPosition(f"no{b}", frozenset(k for k in ladder if k != b), cost=no_cost)
        for b in ladder
    ]
    joint = joint_kelly_fractions(positions, probs, total_fraction_cap=1.0)
    joint_vec = [joint[p.key] for p in positions]

    # Isolated scalar Kelly per NO: win prob 2/3, win_return (1-0.6)/0.6=0.667,
    # f* = p - q/b = 2/3 - (1/3)/0.667 = 0.667 - 0.5 = 0.167 each.
    isolated_vec = [0.167] * 3
    assert expected_log_growth(positions, joint_vec, probs) >= expected_log_growth(
        positions, isolated_vec, probs
    ) - 1e-9


def test_total_fraction_cap_is_respected():
    pos = [
        JointPosition("bin1", frozenset({1}), cost=0.20),  # very cheap, high edge
        JointPosition("bin2", frozenset({2}), cost=0.20),
    ]
    probs = {1: 0.5, 2: 0.5}
    fractions = joint_kelly_fractions(pos, probs, total_fraction_cap=0.3)
    assert sum(fractions.values()) <= 0.3 + 1e-6


def test_expected_log_growth_is_negative_infinity_on_ruinous_stake():
    # Staking the whole bankroll on one bin ruins the losing scenario.
    pos = [JointPosition("a", frozenset({1}), cost=0.5)]
    probs = {1: 0.6, 0: 0.4}
    assert expected_log_growth(pos, [1.0], probs) == -math.inf


def test_empty_positions_return_empty():
    assert joint_kelly_fractions([], {1: 1.0}) == {}


def test_build_joint_positions_maps_sides_and_normalizes_scenarios():
    bin_keys = ["b1", "b2", "b3"]
    yes_probs = {"b1": 0.2, "b2": 0.3, "b3": 0.1}  # sums to 0.6 -> normalized
    legs = [
        ("yes_b2", "b2", "YES", 0.35),
        ("no_b1", "b1", "NO", 0.70),
    ]
    positions, scenario_probs = build_joint_positions(bin_keys, yes_probs, legs)
    # Scenario distribution is normalized over the ladder.
    assert sum(scenario_probs.values()) == approx(1.0)
    assert scenario_probs[1] == approx(0.3 / 0.6)
    by_key = {p.key: p for p in positions}
    # YES on b2 (index 1) wins only in scenario 1.
    assert by_key["yes_b2"].win_scenarios == frozenset({1})
    # NO on b1 (index 0) wins in every scenario EXCEPT 0.
    assert by_key["no_b1"].win_scenarios == frozenset({1, 2})


def test_build_and_solve_end_to_end_prefers_the_underpriced_bin():
    bin_keys = ["b1", "b2"]
    yes_probs = {"b1": 0.7, "b2": 0.3}
    # YES on b1 is underpriced (cost 0.5 vs true 0.7); YES on b2 is overpriced.
    legs = [("yes_b1", "b1", "YES", 0.5), ("yes_b2", "b2", "YES", 0.5)]
    positions, scenario_probs = build_joint_positions(bin_keys, yes_probs, legs)
    fractions = joint_kelly_fractions(positions, scenario_probs, total_fraction_cap=1.0)
    assert fractions["yes_b1"] > fractions["yes_b2"]
