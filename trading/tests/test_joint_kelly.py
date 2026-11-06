"""Tests for the joint Kelly allocator across mutually exclusive bins (Phase 2c)."""

import math

from pytest import approx

from sfo_kalshi_quant.joint_kelly import (
    JointPosition,
    build_joint_positions,
    expected_log_growth,
    joint_kelly_fractions,
    joint_kelly_optimum,
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


# --- joint_kelly_optimum: the exact solve behind the portfolio's joint trims ---


def _marginal_growth(positions, fractions, scenario_probs):
    """d E[log W] / d fraction for each position (the KKT gradient)."""

    gradient = {position.key: 0.0 for position in positions}
    for scenario, prob in scenario_probs.items():
        wealth = 1.0
        for position in positions:
            if scenario in position.win_scenarios:
                wealth += fractions[position.key] * position.win_return_per_fraction()
            else:
                wealth -= fractions[position.key]
        for position in positions:
            ret = position.win_return_per_fraction() if scenario in position.win_scenarios else -1.0
            gradient[position.key] += prob * ret / wealth
    return gradient


def test_optimum_matches_scalar_kelly_exactly():
    even_money = [JointPosition("a", frozenset({1}), cost=0.5)]
    assert joint_kelly_optimum(even_money, {1: 0.6, 0: 0.4})["a"] == approx(0.2, abs=1e-9)
    # An expensive NO favorite: (q - c) / (1 - c) = (0.999 - 0.98) / 0.02 = 0.95.
    favorite = [JointPosition("no", frozenset({1}), cost=0.98)]
    assert joint_kelly_optimum(favorite, {0: 0.001, 1: 0.999})["no"] == approx(0.95, abs=1e-9)


def test_optimum_stakes_nothing_without_an_edge():
    pos = [JointPosition("a", frozenset({1}), cost=0.5)]
    assert joint_kelly_optimum(pos, {1: 0.5, 0: 0.5}) == {"a": 0.0}
    overpriced = [JointPosition("a", frozenset({1}), cost=0.6)]
    assert joint_kelly_optimum(overpriced, {1: 0.4, 0: 0.6}) == {"a": 0.0}


def test_optimum_finds_the_split_the_gradient_solver_stops_short_of():
    # Cheap YES legs beside an expensive NO: the fixed-step gradient solver
    # stops at 0.41 on the NO, under half its optimal stake, although its own
    # cap (1.0) is nowhere near binding.
    ladder = {"b0": 0.0378, "b1": 0.0006, "b2": 0.1931, "b3": 0.2741, "b4": 0.4458, "b5": 0.0487}
    legs = [
        ("yes_b2", "b2", "YES", 0.17),
        ("no_b1", "b1", "NO", 0.98),
        ("yes_b0", "b0", "YES", 0.02),
        ("no_b4", "b4", "NO", 0.57),
        ("yes_b5", "b5", "YES", 0.06),
    ]
    positions, scenario_probs = build_joint_positions(sorted(ladder), ladder, legs)
    gradient_split = joint_kelly_fractions(positions, scenario_probs, total_fraction_cap=1.0)
    optimum = joint_kelly_optimum(positions, scenario_probs)

    assert optimum is not None
    assert sum(optimum.values()) < 1.0  # interior: the bound plays no part
    assert gradient_split["no_b1"] < 0.45
    assert optimum["no_b1"] == approx(0.924, abs=1e-3)

    def growth(split):
        return expected_log_growth(positions, [split[p.key] for p in positions], scenario_probs)

    assert growth(optimum) > growth(gradient_split) + 0.008
    # KKT: staked legs have zero marginal growth; unstaked ones have none to gain.
    marginal = _marginal_growth(positions, optimum, scenario_probs)
    for key, fraction in optimum.items():
        if fraction > 0.0:
            assert marginal[key] == approx(0.0, abs=1e-9)
        else:
            assert marginal[key] <= 1e-9


def test_optimum_is_none_when_it_would_stake_past_the_bound():
    favorite = [JointPosition("no", frozenset({1}), cost=0.98)]
    probs = {0: 0.001, 1: 0.999}
    assert joint_kelly_optimum(favorite, probs, max_total_fraction=1.0) is not None
    assert joint_kelly_optimum(favorite, probs, max_total_fraction=0.5) is None


def test_optimum_is_none_for_unbounded_growth():
    # YES on both bins of a two-bin ladder for 0.80 in all is a sure profit.
    pos = [
        JointPosition("a", frozenset({0}), cost=0.40),
        JointPosition("b", frozenset({1}), cost=0.40),
    ]
    assert joint_kelly_optimum(pos, {0: 0.5, 1: 0.5}) is None


def test_optimum_is_none_when_the_split_is_not_unique():
    # YES on bins 0, 1 and 3 pays exactly what NO on bin 2 pays, for the same
    # 0.64, so stake can move between them without changing any outcome.
    probs = {0: 0.40, 1: 0.08, 2: 0.25, 3: 0.27}
    yes_legs = [
        JointPosition("yes0", frozenset({0}), cost=0.36),
        JointPosition("yes1", frozenset({1}), cost=0.06),
        JointPosition("yes3", frozenset({3}), cost=0.22),
    ]
    replica = JointPosition("no2", frozenset({0, 1, 3}), cost=0.64)
    assert joint_kelly_optimum([*yes_legs, replica], probs) is None
    # A cent dearer the NO is no longer a replica: one optimum, NO unstaked.
    dearer = JointPosition("no2", frozenset({0, 1, 3}), cost=0.65)
    optimum = joint_kelly_optimum([*yes_legs, dearer], probs)
    assert optimum is not None
    assert optimum["no2"] == 0.0


def test_optimum_of_no_positions_is_empty():
    assert joint_kelly_optimum([], {1: 1.0}) == {}
