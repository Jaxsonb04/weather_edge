"""The joint-Kelly resize may only trim a live leg, and only by its joint ratio.

Audit 2026-11-01 (joint-Kelly resize finding). The live profile turns
``joint_kelly_enabled`` on, and ``portfolio._joint_resize_directional``
replaced every directional leg's contract count with its share of a FULL-Kelly
solve on the raw point probabilities, bounded only by the day's whole remaining
directional budget (8% of bankroll at the first scan of the day). None of the
per-leg sizer's controls reached it -- fractional Kelly 0.30, the LCB-weighted
sizing probability, the posterior trust multiplier, the uncertainty clamp, the
3% position-risk cap -- so a zero-edge_lcb NO the sizer put at 6 contracts left
the allocator at 93 and was placed at 34 ($29.21, the $30 account clamp). The
resized decision also kept the per-leg ``binding_constraint``, so the journal
blamed the Kelly budget for a size Kelly never chose.

Capping the resize at the per-leg size was not enough. The solve's cap was
still the budget LEFT for the day, so late in a day's scans the solve split that
leftover between an event's legs and trimmed whichever leg came up short. The
trim was stamped as a joint-Kelly hedge, though the legs fit.

The resize now scales each leg's per-leg target by its joint ratio: its joint
full-Kelly stake over its stand-alone stake on the same ladder. It only ever
trims. The solve's bound is a fixed 1.0 (no leverage), and a bound-limited or
non-unique solve leaves every leg at its per-leg size.
"""

from __future__ import annotations

import math
import random
from dataclasses import replace

import pytest

from sfo_kalshi_quant.config import strategy_config_for_profile
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.fees import kelly_fraction_spent
from sfo_kalshi_quant.joint_kelly import build_joint_positions, joint_kelly_optimum
from sfo_kalshi_quant.models import BucketProbability, MarketBin, TradeDecision
from sfo_kalshi_quant.paper import PaperTrader
from sfo_kalshi_quant.portfolio import allocate_portfolio, portfolio_limits_for_profile
from sfo_kalshi_quant.posterior_kelly import CohortRecord, PosteriorKellyModel
from sfo_kalshi_quant.risk import TradeEvaluator

EVENT = "KXHIGHAUS-26NOV05"
TARGET_DATE = "2026-11-05"
BANKROLL = 1000.0
RUN_ID = "PF-nov01-kelly-size"
# Six-bin Austin ladder (YES probability per bin, summing to 1); each test sets
# the traded bin's own probability on top of it.
BASE_LADDER = {
    "T86": 0.05,
    "B86.5": 0.15,
    "B88.5": 0.40,
    "B90.5": 0.12,
    "B92.5": 0.20,
    "T93": 0.08,
}
DAILY_BUDGET = portfolio_limits_for_profile("live", BANKROLL).max_daily_loss  # $80


def _market(suffix: str, floor: float, cap: float, *, yes_bid: float, yes_ask: float) -> MarketBin:
    size = 500.0
    return MarketBin(
        ticker=f"{EVENT}-{suffix}",
        event_ticker=EVENT,
        title=f"{floor:g}° to {cap:g}°",
        yes_sub_title=f"{floor:g}° to {cap:g}°",
        strike_type="between",
        floor_strike=floor,
        cap_strike=cap,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=round(1.0 - yes_ask, 2),
        no_ask=round(1.0 - yes_bid, 2),
        yes_bid_size=size,
        yes_ask_size=size,
        status="active",
        raw={"no_bid_size_fp": size, "no_ask_size_fp": size},
    )


def _probability(market: MarketBin, p_yes: float, half_width: float) -> BucketProbability:
    return BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=p_yes,
        lower_confidence=p_yes - half_width,
        upper_confidence=p_yes + half_width,
        empirical_probability=p_yes,
        normal_probability=p_yes,
        effective_n=200,
        residual_probability=p_yes,
        model_probability=p_yes,
        market_probability=1.0 - market.no_ask,
    )


def _ladder(traded: dict[str, float]) -> dict[str, float]:
    ladder = {f"{EVENT}-{suffix}": p for suffix, p in BASE_LADDER.items()}
    ladder.update(traded)
    return ladder


def _live_evaluator() -> TradeEvaluator:
    config = strategy_config_for_profile("live")
    # An unproven cohort (no settled history) sits at the live trust floor, 0.4.
    floor_model = PosteriorKellyModel(
        cohort_records={},
        overall=CohortRecord.empty(),
        floor=config.posterior_mean_kelly_floor,
    )
    return TradeEvaluator(config, sizing_model=floor_model)


def _live_no_leg(
    *,
    yes_bid: float,
    yes_ask: float,
    p_yes: float,
    half_width: float,
    forecast_high_f: float | None = None,
) -> tuple[TradeDecision, dict[str, float]]:
    """A live NO on the 90-91F bin, sized by the real per-leg evaluator."""

    market = _market("B90.5", 90, 91, yes_bid=yes_bid, yes_ask=yes_ask)
    decision = _live_evaluator().evaluate_market(
        market,
        _probability(market, p_yes, half_width),
        bankroll=BANKROLL,
        side="NO",
        forecast_high_f=forecast_high_f,
    )
    return decision, _ladder({market.ticker: p_yes})


def _live_kelly_fraction(probability: float, probability_lcb: float, cost: float) -> float:
    """risk.py's live kelly_fraction for an unproven cohort (trust floor 0.4)."""

    config = strategy_config_for_profile("live")
    sizing_probability = (
        config.kelly_lcb_weight * probability_lcb
        + (1.0 - config.kelly_lcb_weight) * probability
    )
    return (
        kelly_fraction_spent(sizing_probability, cost)
        * config.fractional_kelly
        * config.posterior_mean_kelly_floor
    )


def _synthetic_leg(
    suffix: str,
    floor: float,
    cap: float,
    *,
    side: str,
    probability: float,
    probability_lcb: float,
    cost: float,
    yes_bid: float,
    yes_ask: float,
    contracts: float,
    binding_constraint: str,
) -> TradeDecision:
    """A live leg carrying the kelly_fraction risk.py would stamp on it.

    That is full Kelly on the 0.6-LCB-weighted sizing probability x fractional
    Kelly 0.30 x the 0.4 trust floor. Used where the real evaluator will not
    approve the leg: no live YES has ever passed its signal gates.
    """

    market = _market(suffix, floor, cap, yes_bid=yes_bid, yes_ask=yes_ask)
    kelly = _live_kelly_fraction(probability, probability_lcb, cost)
    return TradeDecision(
        ticker=market.ticker,
        label=market.yes_sub_title,
        action=f"BUY_{side}",
        approved=True,
        probability=probability,
        probability_lcb=probability_lcb,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        spread=0.01,
        fee_per_contract=0.01,
        cost_per_contract=cost,
        edge=probability - cost,
        edge_lcb=probability_lcb - cost,
        kelly_fraction=kelly,
        recommended_contracts=float(contracts),
        expected_profit=(probability - cost) * contracts,
        reasons=[],
        side=side,
        strike_type=market.strike_type,
        floor_strike=market.floor_strike,
        cap_strike=market.cap_strike,
        trade_quality_score=60.0,
        binding_constraint=binding_constraint,
    )


def _allocate(
    decisions: list[TradeDecision],
    ladder: dict[str, float],
    *,
    joint: bool,
    existing_directional_spend: float = 0.0,
):
    return allocate_portfolio(
        decisions,
        bankroll=BANKROLL,
        risk_profile="live",
        run_id=RUN_ID,
        bin_yes_probs=ladder,
        joint_kelly_enabled=joint,
        existing_directional_spend=existing_directional_spend,
    )


def _spend(decision: TradeDecision) -> float:
    return decision.recommended_contracts * decision.cost_per_contract


def _joint_solve(
    decisions: list[TradeDecision],
    ladder: dict[str, float],
    *,
    max_total_fraction: float = 1.0,
) -> tuple[dict[str, float], dict[str, float]]:
    """Test oracle: (joint full-Kelly spend in $, joint ratio) per ticker."""

    specs = [(d.ticker, d.ticker, d.side, d.cost_per_contract) for d in decisions]
    positions, scenario_probs = build_joint_positions(sorted(ladder), ladder, specs)
    joint = joint_kelly_optimum(
        positions, scenario_probs, max_total_fraction=max_total_fraction
    )
    assert joint is not None
    ratios = {}
    for position in positions:
        alone = joint_kelly_optimum(
            [position], scenario_probs, max_total_fraction=max_total_fraction
        )
        assert alone is not None and alone[position.key] > 0.0
        ratios[position.key] = joint[position.key] / alone[position.key]
    return {key: fraction * BANKROLL for key, fraction in joint.items()}, ratios


# Thin-edge live NO legs from the audit repro (NO ask 0.85 / 0.84, trust at the
# 0.4 floor). Per leg they size to 12, 6, 11 and 29 contracts; before the fix
# each left the allocator at 93-94 contracts (~$80, the whole daily budget).
THIN_EDGE_LIVE_NO_LEGS = [
    pytest.param(0.15, 0.16, 0.125, 0.015, 75.0, id="zero-edge-lcb-comfort-boost"),
    pytest.param(0.15, 0.16, 0.125, 0.015, None, id="zero-edge-lcb"),
    pytest.param(0.15, 0.16, 0.120, 0.015, None, id="edge-lcb-half-cent"),
    pytest.param(0.16, 0.17, 0.078, 0.069, None, id="production-shaped"),
]


@pytest.mark.parametrize(
    "yes_bid,yes_ask,p_yes,half_width,forecast_high_f", THIN_EDGE_LIVE_NO_LEGS
)
def test_joint_kelly_never_grows_a_live_leg_past_its_per_leg_size(
    yes_bid: float,
    yes_ask: float,
    p_yes: float,
    half_width: float,
    forecast_high_f: float | None,
) -> None:
    # The resize runs for the live profile; this is the production path.
    assert strategy_config_for_profile("live").joint_kelly_enabled is True
    decision, ladder = _live_no_leg(
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        p_yes=p_yes,
        half_width=half_width,
        forecast_high_f=forecast_high_f,
    )
    assert decision.approved
    assert decision.edge_lcb >= 0.0  # the NO core sleeve, not exploration
    assert decision.recommended_contracts > 0
    # Not vacuous: on its own the full-Kelly solve wants far more than the
    # per-leg sizer chose, which is what the old resize handed the leg.
    joint_spend, _ = _joint_solve([decision], ladder)
    assert joint_spend[decision.ticker] > 2.0 * _spend(decision)

    off = _allocate([decision], ladder, joint=False)
    on = _allocate([decision], ladder, joint=True)

    assert [leg.decision.recommended_contracts for leg in on.legs] == [
        decision.recommended_contracts
    ]
    assert on.total_spend == pytest.approx(off.total_spend)
    # Kept untouched, so kelly_fraction and binding_constraint still describe
    # the size that is actually requested.
    assert [leg.decision for leg in on.legs] == [leg.decision for leg in off.legs]


def test_joint_kelly_leaves_two_diversifying_no_legs_untouched() -> None:
    evaluator = _live_evaluator()
    first = _market("B90.5", 90, 91, yes_bid=0.15, yes_ask=0.16)
    second = _market("B86.5", 86, 87, yes_bid=0.15, yes_ask=0.16)
    decisions = [
        evaluator.evaluate_market(
            market, _probability(market, 0.125, 0.015), bankroll=BANKROLL, side="NO"
        )
        for market in (first, second)
    ]
    assert all(decision.approved for decision in decisions)
    ladder = _ladder({first.ticker: 0.125, second.ticker: 0.125})
    # NO legs on different bins lose in different scenarios, so together each
    # deserves MORE than alone (ratio > 1); trim-only leaves both as sized.
    _, ratios = _joint_solve(decisions, ladder)
    assert all(ratio > 1.0 for ratio in ratios.values())

    off = _allocate(decisions, ladder, joint=False)
    on = _allocate(decisions, ladder, joint=True)

    # Was 46 contracts ($39.56) per leg against a per-leg 6.
    assert [leg.decision for leg in on.legs] == [leg.decision for leg in off.legs]
    assert on.total_spend == pytest.approx(sum(_spend(d) for d in decisions))


@pytest.mark.parametrize("budget_left", [80.0, 40.0, 36.0, 35.0, 34.3, 34.21])
def test_joint_kelly_two_leg_sizes_do_not_depend_on_scan_order(budget_left: float) -> None:
    # Reviewer repro. Per leg, NO 86-87F is 35 contracts ($29.05) and NO
    # 90-91F is 6 ($5.16): $34.21 together. The solve's cap was the budget left
    # for the day, so with $36, $35 or $34.30 left it split that leftover and
    # the 86-87F leg came out at 34, 33 or 32 contracts, stamped joint_kelly,
    # although both legs fit. The budget left must not change a leg's size.
    evaluator = _live_evaluator()
    wide = _market("B86.5", 86, 87, yes_bid=0.19, yes_ask=0.20)
    thin = _market("B90.5", 90, 91, yes_bid=0.15, yes_ask=0.16)
    decisions = [
        evaluator.evaluate_market(
            wide, _probability(wide, 0.12, 0.015), bankroll=BANKROLL, side="NO"
        ),
        evaluator.evaluate_market(
            thin, _probability(thin, 0.125, 0.015), bankroll=BANKROLL, side="NO"
        ),
    ]
    assert [decision.recommended_contracts for decision in decisions] == [35, 6]
    ladder = _ladder({wide.ticker: 0.12, thin.ticker: 0.125})
    per_leg_total = sum(_spend(decision) for decision in decisions)
    assert per_leg_total == pytest.approx(34.21)
    # Both legs fit at every position (the greedy pass allows 1e-9 of float
    # slack, and 35 x 0.83 + 6 x 0.86 sums to 34.21000000000001).
    assert per_leg_total <= budget_left + 1e-9
    spent_earlier = DAILY_BUDGET - budget_left

    off = _allocate(decisions, ladder, joint=False, existing_directional_spend=spent_earlier)
    on = _allocate(decisions, ladder, joint=True, existing_directional_spend=spent_earlier)

    assert sorted(
        (leg.decision.ticker, leg.decision.recommended_contracts) for leg in on.legs
    ) == sorted((decision.ticker, decision.recommended_contracts) for decision in decisions)
    assert [leg.decision for leg in on.legs] == [leg.decision for leg in off.legs]
    assert all(leg.decision.binding_constraint == "kelly_budget" for leg in on.legs)


def _overlapping_pair(
    *, no_contracts: float = 25
) -> tuple[list[TradeDecision], dict[str, float]]:
    """A YES on the 88-89F forecast bin and a NO on the adjacent 90-91F bin.

    Both lose when the high lands in 90-91F, so each partly duplicates the
    other's risk; on BASE_LADDER the joint solve keeps 74% of the YES's
    stand-alone stake and 77% of the NO's.
    """

    yes_leg = _synthetic_leg(
        "B88.5",
        88,
        89,
        side="YES",
        probability=0.40,
        probability_lcb=0.37,
        cost=0.35,
        yes_bid=0.33,
        yes_ask=0.34,
        contracts=14,  # $4.90 under the $5 live YES cap; Kelly budget $5.91
        binding_constraint="yes_estimation_shrink",
    )
    no_leg = _synthetic_leg(
        "B90.5",
        90,
        91,
        side="NO",
        probability=0.88,
        probability_lcb=0.86,
        cost=0.84,
        yes_bid=0.15,
        yes_ask=0.16,
        contracts=no_contracts,  # 25 = $21.00, its Kelly budget
        binding_constraint="kelly_budget",
    )
    return [yes_leg, no_leg], _ladder({})


@pytest.mark.parametrize("spent_earlier", [0.0, 40.0, 54.0])
def test_joint_kelly_trims_an_overlapping_pair_by_its_scaled_joint_ratio(
    spent_earlier: float,
) -> None:
    decisions, ladder = _overlapping_pair()
    yes_leg, no_leg = decisions
    assert _spend(yes_leg) + _spend(no_leg) <= DAILY_BUDGET - spent_earlier  # both fit
    joint_spend, ratios = _joint_solve(decisions, ladder)
    assert ratios[yes_leg.ticker] == pytest.approx(0.743, abs=1e-3)
    assert ratios[no_leg.ticker] == pytest.approx(0.771, abs=1e-3)
    # The scaling sets the size, not an unscaled share: unscaled, the joint
    # solve wants $57 of YES and $193 of NO, so the rejected trim-only resize
    # trimmed neither leg at the first scan.
    for decision in decisions:
        assert joint_spend[decision.ticker] > 2.0 * _spend(decision)

    plan = _allocate(decisions, ladder, joint=True, existing_directional_spend=spent_earlier)

    by_ticker = {leg.decision.ticker: leg.decision for leg in plan.legs}
    # 0.743 x the YES leg's $5.91 Kelly budget = $4.39 -> 13 contracts (was 14);
    # 0.771 x the NO leg's $21.00 = $16.20 -> 19 contracts (was 25). The same
    # at every point in the day's scan order.
    assert by_ticker[yes_leg.ticker].recommended_contracts == 13
    assert by_ticker[no_leg.ticker].recommended_contracts == 19
    for original in decisions:
        trimmed = by_ticker[original.ticker]
        per_leg_target = max(original.kelly_fraction * BANKROLL, _spend(original))
        assert trimmed.recommended_contracts == round(
            ratios[original.ticker] * per_leg_target / original.cost_per_contract
        )
        assert trimmed.binding_constraint == "joint_kelly"
        assert trimmed.kelly_fraction == original.kelly_fraction
        assert trimmed.expected_profit == pytest.approx(
            original.edge * trimmed.recommended_contracts
        )


@pytest.mark.parametrize(
    "no_contracts,joint_contracts",
    [
        # An event-cap scale left 19.6 contracts, which placement truncates to
        # 19: the count the 0.771 joint ratio asks for. The order is the same
        # either way, so the leg and its journal stay as the per-leg sizer set
        # them.
        pytest.param(19.6, None, id="same-whole-count"),
        # 20.4 places 20, so the joint ratio really cuts it, to 19.
        pytest.param(20.4, 19.0, id="real-trim"),
    ],
)
def test_joint_kelly_compares_whole_contracts_on_a_fractional_leg(
    no_contracts: float, joint_contracts: float | None
) -> None:
    decisions, ladder = _overlapping_pair(no_contracts=no_contracts)
    no_ticker = decisions[1].ticker

    off = _allocate(decisions, ladder, joint=False)
    on = _allocate(decisions, ladder, joint=True)

    off_leg = {leg.decision.ticker: leg.decision for leg in off.legs}[no_ticker]
    on_leg = {leg.decision.ticker: leg.decision for leg in on.legs}[no_ticker]
    if joint_contracts is None:
        assert on_leg == off_leg
    else:
        assert on_leg.recommended_contracts == joint_contracts
        assert on_leg.binding_constraint == "joint_kelly"


def test_joint_kelly_does_not_undo_a_comfort_boost() -> None:
    # Two far-tail NO legs, each comfort-boosted to 12 contracts ($10.32) on a
    # $5.14 Kelly budget. They diversify (ratio 1.19), so nothing should trim
    # them. Scaling the Kelly budget alone, which the boost never touches, would
    # cut both to 7.
    evaluator = _live_evaluator()
    markets = [
        _market("B90.5", 90, 91, yes_bid=0.15, yes_ask=0.16),
        _market("B86.5", 86, 87, yes_bid=0.15, yes_ask=0.16),
    ]
    decisions = [
        evaluator.evaluate_market(
            market,
            _probability(market, 0.125, 0.015),
            bankroll=BANKROLL,
            side="NO",
            forecast_high_f=75.0,
        )
        for market in markets
    ]
    ladder = _ladder({market.ticker: 0.125 for market in markets})
    _, ratios = _joint_solve(decisions, ladder)
    for decision in decisions:
        assert decision.approved and decision.recommended_contracts == 12
        kelly_budget = decision.kelly_fraction * BANKROLL
        assert _spend(decision) > 1.9 * kelly_budget  # the boost is real
        assert 1.0 < ratios[decision.ticker] < _spend(decision) / kelly_budget

    off = _allocate(decisions, ladder, joint=False)
    on = _allocate(decisions, ladder, joint=True)

    assert [leg.decision for leg in on.legs] == [leg.decision for leg in off.legs]


def test_joint_kelly_trims_a_leg_it_stakes_nothing_on_to_one_contract() -> None:
    # On this four-bin ladder the YES on 88-89F covers the NO on 90-91F so
    # well that the joint solve stakes nothing on the NO. The resize never
    # drops a leg the greedy pass selected, but it no longer keeps that leg at
    # its full per-leg size either.
    yes_leg = _synthetic_leg(
        "B88.5",
        88,
        89,
        side="YES",
        probability=0.452,
        probability_lcb=0.42,
        cost=0.40,
        yes_bid=0.38,
        yes_ask=0.39,
        contracts=12,  # $4.80 under the $5 live YES cap
        binding_constraint="yes_estimation_shrink",
    )
    no_leg = _synthetic_leg(
        "B90.5",
        90,
        91,
        side="NO",
        probability=0.531,
        probability_lcb=0.51,
        cost=0.50,
        yes_bid=0.49,
        yes_ask=0.50,
        contracts=9,  # $4.50, its Kelly budget
        binding_constraint="kelly_budget",
    )
    ladder = {
        f"{EVENT}-T86": 0.031,
        f"{EVENT}-B86.5": 0.048,
        yes_leg.ticker: 0.452,
        no_leg.ticker: 0.469,
    }
    joint_spend, ratios = _joint_solve([yes_leg, no_leg], ladder)
    assert joint_spend[no_leg.ticker] == 0.0
    assert ratios[yes_leg.ticker] == pytest.approx(1.0)

    plan = _allocate([yes_leg, no_leg], ladder, joint=True)

    by_ticker = {leg.decision.ticker: leg.decision for leg in plan.legs}
    assert by_ticker[no_leg.ticker].recommended_contracts == 1
    assert by_ticker[no_leg.ticker].binding_constraint == "joint_kelly"
    # The YES keeps its whole stand-alone share, so it is left untouched.
    assert by_ticker[yes_leg.ticker].recommended_contracts == 12
    assert by_ticker[yes_leg.ticker].binding_constraint == "yes_estimation_shrink"


def test_joint_kelly_keeps_per_leg_sizes_when_the_full_kelly_book_is_bound_limited() -> None:
    # Unbounded, the full-Kelly book on these three legs stakes 129% of
    # bankroll, and in that solve the YES's joint ratio is 0. Past the
    # no-leverage bound the split says nothing reliable about hedging, so the
    # resize keeps every leg at its per-leg size instead of acting on it.
    decisions = [
        _synthetic_leg(
            "B88.5",
            88,
            89,
            side="YES",
            probability=0.40,
            probability_lcb=0.36,
            cost=0.26,
            yes_bid=0.24,
            yes_ask=0.25,
            contracts=19,  # $4.94 under the $5 live YES cap
            binding_constraint="yes_estimation_shrink",
        ),
        _synthetic_leg(
            "B90.5",
            90,
            91,
            side="NO",
            probability=0.88,
            probability_lcb=0.85,
            cost=0.63,
            yes_bid=0.36,
            yes_ask=0.37,
            contracts=47,  # $29.61 under the 3% position-risk cap
            binding_constraint="position_risk_cap",
        ),
        _synthetic_leg(
            "B86.5",
            86,
            87,
            side="NO",
            probability=0.85,
            probability_lcb=0.845,
            cost=0.84,
            yes_bid=0.15,
            yes_ask=0.16,
            contracts=6,  # $5.04, its Kelly budget
            binding_constraint="kelly_budget",
        ),
    ]
    ladder = _ladder({})
    positions, scenario_probs = build_joint_positions(
        sorted(ladder),
        ladder,
        [(d.ticker, d.ticker, d.side, d.cost_per_contract) for d in decisions],
    )
    assert joint_kelly_optimum(positions, scenario_probs) is None
    unbounded_spend, unbounded_ratios = _joint_solve(decisions, ladder, max_total_fraction=10.0)
    assert sum(unbounded_spend.values()) > BANKROLL
    assert unbounded_ratios[decisions[0].ticker] == pytest.approx(0.0)

    off = _allocate(decisions, ladder, joint=False)
    on = _allocate(decisions, ladder, joint=True)

    assert len(on.legs) == 3
    assert [leg.decision for leg in on.legs] == [leg.decision for leg in off.legs]


def test_joint_kelly_no_longer_places_a_thin_live_leg_at_the_account_clamp(tmp_path) -> None:
    # End to end through the limit-mode paper trader, as production runs it.
    # Before the fix this zero-edge_lcb leg (per-leg 6 contracts, $5.16) was
    # placed at 34 contracts / $29.21 -- the $30 account clamp, not Kelly.
    config = strategy_config_for_profile("live")
    decision, ladder = _live_no_leg(yes_bid=0.15, yes_ask=0.16, p_yes=0.125, half_width=0.015)
    plan = _allocate([decision], ladder, joint=True)

    store = PaperStore(tmp_path / "paper.db")
    trader = PaperTrader(
        store,
        config,
        risk_profile="live",
        entry_mode="limit",
        series_ticker="KXHIGHAUS",
    )
    trader.place_approved(TARGET_DATE, plan.decisions, bankroll=BANKROLL)

    orders = store.open_paper_orders()
    assert len(orders) == 1
    assert orders[0]["contracts"] <= decision.recommended_contracts
    assert orders[0]["contracts"] * orders[0]["cost_per_contract"] <= _spend(decision) + 1e-9


# Settlement range of each BASE_LADDER bin, for the seeded property test.
_BIN_BOUNDS = {
    "T86": (84, 85),
    "B86.5": (86, 87),
    "B88.5": (88, 89),
    "B90.5": (90, 91),
    "B92.5": (92, 93),
    "T93": (94, 95),
}


def _random_live_event(rng: random.Random) -> tuple[list[TradeDecision], dict[str, float]]:
    """A ladder plus up to six thin-edge legs sized like the live per-leg sizer."""

    raw = {suffix: rng.random() ** 2 + 0.02 for suffix in BASE_LADDER}
    total = sum(raw.values())
    ladder = {f"{EVENT}-{suffix}": value / total for suffix, value in raw.items()}
    decisions = []
    for suffix, (floor, cap) in _BIN_BOUNDS.items():
        if rng.random() < 0.4:
            continue
        side = "YES" if rng.random() < 0.3 else "NO"
        p_yes = ladder[f"{EVENT}-{suffix}"]
        probability = p_yes if side == "YES" else 1.0 - p_yes
        cost = round(probability - rng.uniform(0.01, 0.06), 2)
        if not 0.02 <= cost <= 0.97 or probability <= cost:
            continue
        probability_lcb = probability - rng.uniform(0.0, probability - cost)
        kelly_budget = _live_kelly_fraction(probability, probability_lcb, cost) * BANKROLL
        # The 3% position-risk cap, or the $5 live YES cap.
        per_leg_spend = min(kelly_budget, 5.0 if side == "YES" else 30.0)
        raw_contracts = per_leg_spend / cost
        contracts = float(math.floor(raw_contracts) if side == "YES" else round(raw_contracts))
        if rng.random() < 0.25:
            contracts *= rng.uniform(0.6, 1.0)  # an event-cap scale leaves a fraction
        if contracts < 1.0:
            continue
        yes_bid = round(min(0.98, max(0.01, p_yes - 0.01)), 2)
        decisions.append(
            _synthetic_leg(
                suffix,
                floor,
                cap,
                side=side,
                probability=probability,
                probability_lcb=probability_lcb,
                cost=cost,
                yes_bid=yes_bid,
                yes_ask=round(yes_bid + 0.01, 2),
                contracts=contracts,
                binding_constraint="kelly_budget",
            )
        )
    return decisions, ladder


def test_joint_kelly_invariants_on_random_live_events() -> None:
    # The reviewer's fuzz, as a seeded regression: with the joint step on, no
    # leg grows, no leg is added or dropped, a changed leg is a real
    # whole-contract trim stamped joint_kelly, and the result is the same
    # wherever the event falls in the day's scan order. NO-only events, today's
    # live book, are left exactly as sized.
    rng = random.Random(20261101)
    events = no_only_events = trimmed_legs = 0
    for _ in range(300):
        decisions, ladder = _random_live_event(rng)
        total = sum(_spend(decision) for decision in decisions)
        if len(decisions) < 2 or total > DAILY_BUDGET:
            continue
        events += 1
        off = _allocate(decisions, ladder, joint=False)
        # First scan of the day, halfway, and with only these legs' cost left.
        plans = [
            _allocate(decisions, ladder, joint=True, existing_directional_spend=spent)
            for spent in (0.0, (DAILY_BUDGET - total) / 2.0, DAILY_BUDGET - total)
        ]
        first_scan = [leg.decision for leg in plans[0].legs]
        for plan in plans[1:]:
            assert [leg.decision for leg in plan.legs] == first_scan

        off_legs = {leg.decision.ticker: leg.decision for leg in off.legs}
        on_legs = {leg.decision.ticker: leg.decision for leg in plans[0].legs}
        assert on_legs.keys() == off_legs.keys()
        for ticker, on_leg in on_legs.items():
            off_leg = off_legs[ticker]
            if on_leg == off_leg:
                continue
            trimmed_legs += 1
            assert on_leg.binding_constraint == "joint_kelly"
            placed_before = math.floor(off_leg.recommended_contracts + 1e-9)
            assert 1 <= on_leg.recommended_contracts < placed_before
            # Only the size, its expected profit and the journal label move.
            assert replace(
                on_leg,
                recommended_contracts=off_leg.recommended_contracts,
                expected_profit=off_leg.expected_profit,
                binding_constraint=off_leg.binding_constraint,
            ) == off_leg
        assert plans[0].total_spend <= off.total_spend + 1e-9
        if all(decision.side == "NO" for decision in decisions):
            no_only_events += 1
            assert on_legs == off_legs
    # Not vacuous: plenty of events, NO-only ones among them, and real trims.
    assert events >= 150
    assert no_only_events >= 30
    assert trimmed_legs >= 20
