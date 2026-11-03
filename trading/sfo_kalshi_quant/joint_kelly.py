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
Smoczynski & Tomkins 2010). ``joint_kelly_fractions`` does it by projected
gradient ascent under a hard cap on the total stake; ``joint_kelly_optimum``
does it exactly, by projected Newton steps, and returns only a verified
optimum.

Pure and DB-free. Inputs are model scenario probabilities and candidate
positions (each described by the settlement highs where it wins and its cost);
the output is a bankroll fraction per position. The portfolio wiring
(``portfolio._joint_resize_directional``) is opt-in and only ever trims a
leg: it scales the leg's per-leg fractional-Kelly size by its joint stake over
its stand-alone stake, and never raises it. Letting the full-Kelly solve set
sizes bypassed fractional Kelly, the LCB sizing weight and the trust
multiplier (audit 2026-11-01), so the extra growth described above is
deliberately not taken.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_DEFAULT_MAX_ITERS = 500
_DEFAULT_LEARNING_RATE = 0.5
_DEFAULT_TOTAL_FRACTION_CAP = 1.0
_CONVERGENCE_TOL = 1e-9
# joint_kelly_optimum: Newton converges quadratically, so a solve that has not
# met the KKT tolerance within this many steps is reported as unconverged.
_NEWTON_MAX_ITERS = 100
# Largest marginal growth (d E[log W] / d fraction) a staked position may keep,
# and the most an unstaked one may still offer, at an accepted optimum.
_KKT_TOL = 1e-9
# A total stake past this means growth is unbounded (an arbitrage among the
# positions), not a slow solve.
_DIVERGENCE_TOTAL_FRACTION = 1e6


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


def joint_kelly_optimum(
    positions: list[JointPosition],
    scenario_probs: dict[int, float],
    *,
    max_total_fraction: float = _DEFAULT_TOTAL_FRACTION_CAP,
) -> dict[str, float] | None:
    """The exact growth-optimal bankroll fraction per position, or None.

    Maximizes E[log wealth] over fractions >= 0 with projected Newton steps and
    a backtracking line search, then checks the optimality (KKT) conditions:
    every staked position's marginal growth is zero and no unstaked position
    would gain from a stake, both within ``_KKT_TOL``.

    ``joint_kelly_fractions`` takes fixed-size gradient steps under a hard cap.
    It stops early when legs of very different scale share a solve (an
    expensive NO beside a cheap YES) and wherever its cap binds, where the
    split it returns comes from its proportional projection, not from the
    optimum. This function never returns such a split. It returns None when
    the optimum would stake more than ``max_total_fraction`` of bankroll in
    all, when growth is unbounded (an arbitrage among the positions), when
    some combination of the positions is riskless (duplicate or exactly
    offsetting legs, so the optimum is not unique), or when the solve does not
    converge, so a caller can fail safe instead.
    """

    n = len(positions)
    if n == 0:
        return {}
    # (probability, return per unit fraction of each position) per scenario.
    scenarios = [
        (
            prob,
            [
                position.win_return_per_fraction() if scenario in position.win_scenarios else -1.0
                for position in positions
            ],
        )
        for scenario, prob in sorted(scenario_probs.items())
        if prob > 0.0
    ]
    if not scenarios:
        return None

    def growth(fractions: list[float]) -> float:
        total = 0.0
        for prob, returns in scenarios:
            wealth = 1.0 + sum(f * r for f, r in zip(fractions, returns))
            if wealth <= 0.0:
                return -math.inf
            total += prob * math.log(wealth)
        return total

    def gradient_at(fractions: list[float]) -> tuple[list[float], list[float]]:
        wealth = [1.0 + sum(f * r for f, r in zip(fractions, returns)) for _, returns in scenarios]
        gradient = [
            sum(prob * returns[i] / w for (prob, returns), w in zip(scenarios, wealth))
            for i in range(n)
        ]
        return gradient, wealth

    def optimal(fractions: list[float], gradient: list[float]) -> bool:
        return all(
            abs(g) <= _KKT_TOL if f > 0.0 else g <= _KKT_TOL
            for f, g in zip(fractions, gradient)
        )

    def newton_step(
        fractions: list[float], gradient: list[float], wealth: list[float], free: list[int]
    ) -> list[float] | None:
        # Negative Hessian of E[log W] on the free positions (positive
        # semi-definite): sum_k p_k r_i(k) r_j(k) / W_k^2.
        curvature = [
            [
                sum(
                    prob * returns[i] * returns[j] / (w * w)
                    for (prob, returns), w in zip(scenarios, wealth)
                )
                for j in free
            ]
            for i in free
        ]
        return _solve_linear(curvature, [gradient[i] for i in free])

    fractions = [0.0] * n  # the empty book: wealth 1 in every scenario
    objective = 0.0
    for _ in range(_NEWTON_MAX_ITERS):
        gradient, wealth = gradient_at(fractions)
        if optimal(fractions, gradient):
            break
        # Active set: staked positions move, and so does an unstaked one whose
        # marginal growth is positive -- unless Newton would push it below
        # zero, in which case it stays pinned at zero for this step.
        free = [i for i in range(n) if fractions[i] > 0.0 or gradient[i] > _KKT_TOL]
        while True:
            step = newton_step(fractions, gradient, wealth, free)
            if step is None:
                return None
            pinned = {
                i for index, i in enumerate(free) if fractions[i] <= 0.0 and step[index] < 0.0
            }
            if not pinned:
                break
            free = [i for i in free if i not in pinned]
        # Longest step that keeps every stake non-negative (ratio test).
        longest = 1.0
        for index, i in enumerate(free):
            if step[index] < 0.0:
                longest = min(longest, fractions[i] / -step[index])
        # Newton decrement: about twice the growth still available along the
        # step. Once the gain a step predicts is below float noise, the
        # objective can no longer rank it, so it is taken on trust (still never
        # into a ruinous scenario) and the KKT check decides.
        decrement = sum(gradient[i] * step[index] for index, i in enumerate(free))
        scale = longest
        for _ in range(60):
            trial = list(fractions)
            for index, i in enumerate(free):
                moved = fractions[i] + scale * step[index]
                trial[i] = moved if moved > 1e-15 else 0.0
            trial_objective = growth(trial)
            if trial_objective > objective or (
                scale * decrement < 1e-14 and trial_objective > -math.inf
            ):
                break
            scale *= 0.5
        else:
            break  # no ascent left at float precision; the check below decides
        fractions, objective = trial, trial_objective
        if sum(fractions) > _DIVERGENCE_TOTAL_FRACTION:
            return None

    gradient, wealth = gradient_at(fractions)
    if not optimal(fractions, gradient):
        return None
    # Unique? Another optimum would differ along a riskless direction (zero
    # return in every scenario), which can only use staked positions and
    # unstaked ones with zero marginal growth. If those positions' curvature is
    # singular, such a direction exists and this split is one of many.
    tied = [i for i in range(n) if fractions[i] > 0.0 or gradient[i] >= -_KKT_TOL]
    if tied and newton_step(fractions, gradient, wealth, tied) is None:
        return None
    if sum(fractions) > max_total_fraction + 1e-12:
        return None
    return {position.key: fractions[i] for i, position in enumerate(positions)}


def _solve_linear(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
    """Solve ``matrix @ x = rhs`` by Gaussian elimination; None if singular."""

    n = len(rhs)
    rows = [list(row) + [value] for row, value in zip(matrix, rhs)]
    scale = max((abs(rows[i][i]) for i in range(n)), default=0.0)
    if scale <= 0.0:
        return None
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(rows[r][col]))
        if abs(rows[pivot][col]) <= 1e-13 * scale:
            return None
        rows[col], rows[pivot] = rows[pivot], rows[col]
        for r in range(col + 1, n):
            factor = rows[r][col] / rows[col][col]
            if factor:
                for c in range(col, n + 1):
                    rows[r][c] -= factor * rows[col][c]
    solution = [0.0] * n
    for r in range(n - 1, -1, -1):
        tail = sum(rows[r][c] * solution[c] for c in range(r + 1, n))
        solution[r] = (rows[r][n] - tail) / rows[r][r]
    return solution


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
