"""Joint Kelly allocation across mutually exclusive temperature bins (Phase 2c).

The SFO high settles in exactly one integer-degree bin, so YES/NO positions
across the ladder are not independent bets -- they hedge each other across
settlement scenarios. Sizing each bin with the two-outcome Kelly rule in
isolation (what the per-bin evaluator does) therefore leaves growth on the
table: it cannot see that a basket of NO-favorites is really one correlated bet
on "not the forecast bin", nor credit the diversification that lets more of them
sit under the same worst-case-loss cap.

This module solves the true growth-optimal problem -- maximize expected log
wealth over the full scenario set simultaneously (Kelly 1956; Whelan 2025;
Smoczynski & Tomkins 2010) -- by projected gradient ascent on the fraction
vector. It is the principled route to deploying more capital across more bins per
event without raising ruin risk.

Pure and DB-free. Inputs are model scenario probabilities and candidate
positions (each described by the settlement highs where it wins and its cost);
the output is a bankroll fraction per position. Wiring into the portfolio is
elsewhere and opt-in.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_DEFAULT_MAX_ITERS = 500
_DEFAULT_LEARNING_RATE = 0.5
_DEFAULT_TOTAL_FRACTION_CAP = 1.0
_CONVERGENCE_TOL = 1e-9


@dataclass(frozen=True)
class JointPosition:
    """A candidate position: which settlement highs it wins on, and its cost."""

    key: str
    win_scenarios: frozenset[int]
    cost: float  # price paid per contract in (0, 1); breakeven win-probability

    def win_return_per_fraction(self) -> float:
        """Return per unit bankroll fraction when the position wins."""

        # Stake f -> buy f/cost contracts, each pays 1 -> net f*(1-cost)/cost.
        return (1.0 - self.cost) / self.cost


def _wealth_in_scenario(
    positions: list[JointPosition], fractions: list[float], scenario: int
) -> float:
    wealth = 1.0
    for position, fraction in zip(positions, fractions):
        if fraction <= 0.0:
            continue
        if scenario in position.win_scenarios:
            wealth += fraction * position.win_return_per_fraction()
        else:
            wealth -= fraction  # a loss forfeits the whole stake
    return wealth


def expected_log_growth(
    positions: list[JointPosition],
    fractions: list[float],
    scenario_probs: dict[int, float],
) -> float:
    """E[log wealth] over the settlement scenarios (-inf if any scenario ruins)."""

    total = 0.0
    for scenario, prob in scenario_probs.items():
        if prob <= 0.0:
            continue
        wealth = _wealth_in_scenario(positions, fractions, scenario)
        if wealth <= 0.0:
            return -math.inf
        total += prob * math.log(wealth)
    return total


def joint_kelly_fractions(
    positions: list[JointPosition],
    scenario_probs: dict[int, float],
    *,
    total_fraction_cap: float = _DEFAULT_TOTAL_FRACTION_CAP,
    max_iters: int = _DEFAULT_MAX_ITERS,
    learning_rate: float = _DEFAULT_LEARNING_RATE,
) -> dict[str, float]:
    """Growth-optimal bankroll fraction per position via projected gradient ascent.

    Constraints: every fraction >= 0 and their sum <= ``total_fraction_cap``.
    Deterministic (fixed start, no randomness). Returns a fraction per position
    key; positions the optimizer zeroes out are returned as 0.0.
    """

    n = len(positions)
    if n == 0:
        return {}
    # Start flat and safely inside the feasible region.
    start = min(total_fraction_cap / (2.0 * n), 0.25)
    fractions = [start] * n
    step = learning_rate

    prev_obj = expected_log_growth(positions, fractions, scenario_probs)
    for _ in range(max_iters):
        # Gradient of E[log wealth]: d/df_i = sum_k p_k * r_i(k) / wealth(k),
        # where r_i(k) is position i's return-per-fraction in scenario k.
        grad = [0.0] * n
        for scenario, prob in scenario_probs.items():
            if prob <= 0.0:
                continue
            wealth = _wealth_in_scenario(positions, fractions, scenario)
            if wealth <= 0.0:
                wealth = 1e-9
            inv = prob / wealth
            for i, position in enumerate(positions):
                r = position.win_return_per_fraction() if scenario in position.win_scenarios else -1.0
                grad[i] += inv * r

        trial = [max(0.0, fractions[i] + step * grad[i]) for i in range(n)]
        trial = _project_to_cap(trial, total_fraction_cap)
        obj = expected_log_growth(positions, trial, scenario_probs)
        if obj > prev_obj:
            fractions = trial
            if obj - prev_obj < _CONVERGENCE_TOL:
                prev_obj = obj
                break
            prev_obj = obj
        else:
            # Overshot -- halve the step and retry from the current point.
            step *= 0.5
            if step < 1e-6:
                break

    return {position.key: fractions[i] for i, position in enumerate(positions)}


def _project_to_cap(fractions: list[float], cap: float) -> list[float]:
    """Scale down proportionally if the total exceeds the cap (fractions >= 0)."""

    total = sum(fractions)
    if total <= cap or total <= 0.0:
        return fractions
    scale = cap / total
    return [f * scale for f in fractions]


def build_joint_positions(
    bin_keys: list[str],
    bin_yes_probs: dict[str, float],
    legs: list[tuple[str, str, str, float]],
) -> tuple[list[JointPosition], dict[int, float]]:
    """Translate a bin ladder + candidate legs into the solver's inputs.

    ``bin_keys`` is the ordered set of all bins in the event (they define the
    mutually exclusive settlement scenarios). ``bin_yes_probs`` maps each bin to
    the model probability the high settles there (normalized here into a scenario
    distribution). Each leg is ``(leg_key, bin_key, side, cost)``: a YES leg wins
    only in its own bin's scenario, a NO leg wins in every OTHER scenario -- the
    hedging structure the joint solver exploits.
    """

    index = {key: i for i, key in enumerate(bin_keys)}
    total = sum(max(0.0, bin_yes_probs.get(key, 0.0)) for key in bin_keys) or 1.0
    scenario_probs = {
        i: max(0.0, bin_yes_probs.get(key, 0.0)) / total for i, key in enumerate(bin_keys)
    }
    positions: list[JointPosition] = []
    for leg_key, bin_key, side, cost in legs:
        j = index[bin_key]
        if side.upper() == "YES":
            win = frozenset({j})
        else:
            win = frozenset(i for i in range(len(bin_keys)) if i != j)
        positions.append(JointPosition(leg_key, win, cost))
    return positions, scenario_probs
