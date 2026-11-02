"""The joint-Kelly resize must never raise a live leg above its per-leg size.

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

The per-leg size already carries all of that scaling, so it is the ceiling:
the joint step may only trim a leg, never grow it.
"""

from __future__ import annotations

import pytest

from sfo_kalshi_quant.config import strategy_config_for_profile
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.joint_kelly import build_joint_positions, joint_kelly_fractions
from sfo_kalshi_quant.models import BucketProbability, MarketBin, TradeDecision
from sfo_kalshi_quant.paper import PaperTrader
from sfo_kalshi_quant.portfolio import allocate_portfolio, portfolio_limits_for_profile
from sfo_kalshi_quant.posterior_kelly import CohortRecord, PosteriorKellyModel
from sfo_kalshi_quant.risk import TradeEvaluator

EVENT = "KXHIGHAUS-26NOV05"
TARGET_DATE = "2026-11-05"
BANKROLL = 1000.0
RUN_ID = "PF-nov01-kelly-size"
# Six-bin Austin ladder (YES probability per bin); each test sets the traded
# bin's own probability on top of it.
BASE_LADDER = {
    "T86": 0.05,
    "B86.5": 0.15,
    "B88.5": 0.40,
    "B90.5": 0.12,
    "B92.5": 0.20,
    "T93": 0.08,
}


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


def _full_kelly_joint_spend(decision: TradeDecision, ladder: dict[str, float]) -> float:
    """What the joint solver alone wants for this one leg, in dollars."""

    positions, scenario_probs = build_joint_positions(
        sorted(ladder),
        ladder,
        [("leg", decision.ticker, decision.side, decision.cost_per_contract)],
    )
    cap = portfolio_limits_for_profile("live", BANKROLL).max_daily_loss / BANKROLL
    fractions = joint_kelly_fractions(positions, scenario_probs, total_fraction_cap=cap)
    return fractions["leg"] * BANKROLL


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
    # Not vacuous: on its own the full-Kelly joint solve wants far more than
    # the per-leg sizer chose, so only the per-leg ceiling holds the size.
    assert _full_kelly_joint_spend(decision, ladder) > 2.0 * _spend(decision)

    off = _allocate([decision], ladder, joint=False)
    on = _allocate([decision], ladder, joint=True)

    assert [leg.decision.recommended_contracts for leg in on.legs] == [
        decision.recommended_contracts
    ]
    assert on.total_spend == pytest.approx(off.total_spend)
    # Kept untouched, so kelly_fraction and binding_constraint still describe
    # the size that is actually requested.
    assert [leg.decision for leg in on.legs] == [leg.decision for leg in off.legs]


def test_joint_kelly_never_grows_two_thin_legs_in_one_event() -> None:
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

    on = _allocate(decisions, ladder, joint=True)

    per_leg = {decision.ticker: decision.recommended_contracts for decision in decisions}
    assert sorted(leg.decision.ticker for leg in on.legs) == sorted(per_leg)
    for leg in on.legs:
        # Was 46 contracts ($39.56) per leg against a per-leg 6.
        assert leg.decision.recommended_contracts <= per_leg[leg.decision.ticker]
    assert on.total_spend <= sum(_spend(decision) for decision in decisions) + 1e-9


@pytest.mark.parametrize("existing_directional_spend", [0.0, 40.0, 70.0])
def test_joint_kelly_leg_size_does_not_depend_on_scan_order(
    existing_directional_spend: float,
) -> None:
    # The resize's cap was the day's remaining directional budget across ALL
    # cities, so the same leg came out at 93 contracts for the first city
    # scanned and at 46 or 11 for a later one. It is now its per-leg size
    # wherever in the day's scan order it falls.
    decision, ladder = _live_no_leg(yes_bid=0.15, yes_ask=0.16, p_yes=0.125, half_width=0.015)

    on = _allocate(
        [decision],
        ladder,
        joint=True,
        existing_directional_spend=existing_directional_spend,
    )

    assert [leg.decision.recommended_contracts for leg in on.legs] == [
        decision.recommended_contracts
    ]


def test_joint_kelly_still_trims_a_leg_and_journals_the_trim() -> None:
    # The downward role survives: when the joint solve wants LESS than the
    # per-leg size, the leg is trimmed, and the journal names the joint solver
    # rather than the per-leg lever that no longer set the size.
    market = _market("B90.5", 90, 91, yes_bid=0.25, yes_ask=0.26)  # NO ask 0.75
    per_leg = TradeDecision(
        ticker=market.ticker,
        label=market.yes_sub_title,
        action="BUY_NO",
        approved=True,
        probability=0.80,
        probability_lcb=0.77,
        yes_bid=market.yes_bid,
        yes_ask=market.yes_ask,
        spread=0.01,
        fee_per_contract=0.0,
        cost_per_contract=0.75,
        edge=0.05,
        edge_lcb=0.02,
        kelly_fraction=0.03,
        recommended_contracts=40.0,  # $30, the 3% position-risk cap
        expected_profit=2.0,
        reasons=[],
        side="NO",
        strike_type=market.strike_type,
        floor_strike=market.floor_strike,
        cap_strike=market.cap_strike,
        trade_quality_score=60.0,
        binding_constraint="position_risk_cap",
    )
    # The ladder puts the traded bin at 0.245 of a distribution summing to 1,
    # so full Kelly on the NO is (0.755 - 0.75) / 0.25 = 2% = $20 < $30.
    ladder = {
        f"{EVENT}-T86": 0.10,
        f"{EVENT}-B86.5": 0.20,
        f"{EVENT}-B88.5": 0.30,
        market.ticker: 0.245,
        f"{EVENT}-B92.5": 0.10,
        f"{EVENT}-T93": 0.055,
    }
    assert _full_kelly_joint_spend(per_leg, ladder) < _spend(per_leg)

    on = _allocate([per_leg], ladder, joint=True)

    assert len(on.legs) == 1
    trimmed = on.legs[0].decision
    assert 0 < trimmed.recommended_contracts < per_leg.recommended_contracts
    assert trimmed.binding_constraint == "joint_kelly"
    assert trimmed.expected_profit == pytest.approx(
        per_leg.edge * trimmed.recommended_contracts
    )


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
