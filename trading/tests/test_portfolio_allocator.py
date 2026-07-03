from __future__ import annotations

from dataclasses import replace

from sfo_kalshi_quant.arbitrage import build_arbitrage_opportunities
from sfo_kalshi_quant.config import strategy_config_for_profile
from sfo_kalshi_quant.models import MarketBin, TradeDecision
from sfo_kalshi_quant.portfolio import allocate_portfolio, portfolio_limits_for_profile


def _market(
    label: str,
    *,
    ticker: str,
    strike_type: str = "between",
    floor: float | None = 70,
    cap: float | None = 71,
    yes_ask: float = 0.20,
    no_ask: float = 0.75,
    yes_bid: float = 0.18,
    no_bid: float = 0.73,
    yes_size: float = 100.0,
    no_size: float = 100.0,
) -> MarketBin:
    return MarketBin(
        ticker=ticker,
        event_ticker="KXHIGHTSFO-TEST",
        title=label,
        yes_sub_title=label,
        strike_type=strike_type,
        floor_strike=floor,
        cap_strike=cap,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        yes_bid_size=yes_size,
        yes_ask_size=yes_size,
        status="active",
        raw={"no_bid_size_fp": no_size, "no_ask_size_fp": no_size},
    )


def _decision(
    market: MarketBin,
    *,
    side: str,
    spend: float,
    probability: float,
    edge: float,
    edge_lcb: float,
    quality: float = 60.0,
) -> TradeDecision:
    ask = market.side_ask(side)
    contracts = spend / ask
    return TradeDecision(
        ticker=market.ticker,
        label=market.yes_sub_title,
        action=f"BUY_{side}",
        approved=True,
        probability=probability,
        probability_lcb=max(0.0, probability - 0.05),
        yes_bid=market.yes_bid,
        yes_ask=market.yes_ask,
        spread=market.side_spread(side),
        fee_per_contract=0.0,
        cost_per_contract=ask,
        edge=edge,
        edge_lcb=edge_lcb,
        kelly_fraction=0.01,
        recommended_contracts=contracts,
        expected_profit=edge * contracts,
        reasons=[],
        yes_ask_size=market.yes_ask_size,
        side=side,
        entry_bid=market.side_bid(side),
        entry_ask=ask,
        entry_bid_size=market.side_bid_size(side),
        entry_ask_size=market.side_ask_size(side),
        strike_type=market.strike_type,
        floor_strike=market.floor_strike,
        cap_strike=market.cap_strike,
        model_probability=probability,
        market_probability=max(0.0, min(1.0, probability - 0.10)),
        trade_quality_score=quality,
    )


def test_research_portfolio_funds_arbitrage_first_and_caps_yes_sleeve() -> None:
    yes_market = _market("70° to 71°", ticker="KXHIGHTSFO-TEST-B70.5", yes_ask=0.20)
    no_market = _market("74° to 75°", ticker="KXHIGHTSFO-TEST-B74.5", floor=74, cap=75, no_ask=0.80)
    arb_market = _market(
        "72° to 73°",
        ticker="KXHIGHTSFO-TEST-B72.5",
        floor=72,
        cap=73,
        yes_ask=0.45,
        no_ask=0.48,
    )
    arb = next(
        row
        for row in build_arbitrage_opportunities(
            [arb_market],
            config=strategy_config_for_profile("research"),
            bankroll=1000.0,
            max_spend=20.0,
        )
        if row.kind == "BOX_YES_NO"
    )
    yes_candidates = [
        _decision(
            replace(yes_market, ticker=f"{yes_market.ticker}-{idx}"),
            side="YES",
            spend=30.0,
            probability=0.40,
            edge=0.20,
            edge_lcb=0.08,
            quality=80.0 - idx,
        )
        for idx in range(3)
    ]
    no_candidate = _decision(
        no_market,
        side="NO",
        spend=90.0,
        probability=0.92,
        edge=0.12,
        edge_lcb=0.04,
        quality=70.0,
    )

    plan = allocate_portfolio(
        [*yes_candidates, no_candidate],
        arbitrage_opportunities=[arb],
        bankroll=1000.0,
        risk_profile="research",
    )

    assert plan.approved
    assert [leg.sleeve for leg in plan.legs[:2]] == ["arbitrage", "arbitrage"]
    yes_spend = sum(leg.spend for leg in plan.legs if leg.sleeve == "yes_convex")
    assert yes_spend <= 50.0
    assert sum(1 for leg in plan.legs if leg.sleeve == "yes_convex") == 1
    assert any(leg.sleeve == "no_core" for leg in plan.legs)
    assert plan.worst_case_loss <= portfolio_limits_for_profile("research", 1000.0).max_daily_loss


def test_live_portfolio_shrinks_to_moderate_drawdown_cap() -> None:
    markets = [
        _market("72° to 73°", ticker=f"KXHIGHTSFO-TEST-B72.5-{idx}", floor=72, cap=73, no_ask=0.80)
        for idx in range(3)
    ]
    decisions = [
        _decision(market, side="NO", spend=40.0, probability=0.95, edge=0.15, edge_lcb=0.05)
        for market in markets
    ]

    plan = allocate_portfolio(decisions, bankroll=1000.0, risk_profile="live")

    assert plan.approved
    assert plan.total_spend <= 80.0
    assert plan.worst_case_loss <= 80.0
    assert len(plan.legs) == 2
    assert all(leg.sleeve == "no_core" for leg in plan.legs)


def test_research_exploration_sleeve_keeps_informative_positive_edge_trade_small() -> None:
    market = _market("69° to 70°", ticker="KXHIGHTSFO-TEST-B69.5", floor=69, cap=70, yes_ask=0.35)
    candidate = _decision(
        market,
        side="YES",
        spend=80.0,
        probability=0.42,
        edge=0.07,
        edge_lcb=-0.03,
        quality=35.0,
    )

    plan = allocate_portfolio([candidate], bankroll=1000.0, risk_profile="research")

    assert plan.approved
    assert len(plan.legs) == 1
    assert plan.legs[0].sleeve == "research_explore"
    assert plan.legs[0].spend <= 12.5


def test_portfolio_worst_case_includes_settlements_outside_selected_yes_bins() -> None:
    first = _decision(
        _market("68° to 69°", ticker="KXHIGHTSFO-TEST-B68.5", floor=68, cap=69, yes_ask=0.20),
        side="YES",
        spend=40.0,
        probability=0.55,
        edge=0.35,
        edge_lcb=0.10,
        quality=80.0,
    )
    second = _decision(
        _market("74° to 75°", ticker="KXHIGHTSFO-TEST-B74.5", floor=74, cap=75, yes_ask=0.20),
        side="YES",
        spend=40.0,
        probability=0.55,
        edge=0.35,
        edge_lcb=0.10,
        quality=79.0,
    )

    plan = allocate_portfolio([first, second], bankroll=10000.0, risk_profile="research")

    assert len(plan.legs) == 2
    assert plan.worst_case_loss == 80.0


def test_joint_kelly_resizes_directional_legs_within_cap() -> None:
    # Two NO-favorites on different bins; with the ladder provided and joint
    # Kelly on, the portfolio re-sizes them as a hedged basket and stays under
    # the worst-case-loss cap.
    m1 = _market("70° to 71°", ticker="KXHIGHTSFO-TEST-B70.5", floor=70, cap=71, no_ask=0.75)
    m2 = _market("72° to 73°", ticker="KXHIGHTSFO-TEST-B72.5", floor=72, cap=73, no_ask=0.75)
    d1 = _decision(m1, side="NO", spend=50, probability=0.10, edge=0.10, edge_lcb=0.05)
    d2 = _decision(m2, side="NO", spend=50, probability=0.12, edge=0.10, edge_lcb=0.05)
    ladder = {m1.ticker: 0.10, m2.ticker: 0.12, "KXHIGHTSFO-TEST-B74.5": 0.05}

    joint = allocate_portfolio(
        [d1, d2],
        bankroll=1000,
        risk_profile="research",
        bin_yes_probs=ladder,
        joint_kelly_enabled=True,
    )
    assert joint.approved
    limits = portfolio_limits_for_profile("research", 1000)
    assert joint.worst_case_loss <= limits.max_daily_loss + 1e-9
    # Every re-sized leg carries a real position.
    assert all(leg.decision.recommended_contracts > 0 for leg in joint.legs)


def test_joint_kelly_is_noop_without_a_ladder() -> None:
    m1 = _market("70° to 71°", ticker="KXHIGHTSFO-TEST-B70.5", no_ask=0.75)
    d1 = _decision(m1, side="NO", spend=50, probability=0.10, edge=0.10, edge_lcb=0.05)
    off = allocate_portfolio([d1], bankroll=1000, risk_profile="research")
    on_no_ladder = allocate_portfolio(
        [d1], bankroll=1000, risk_profile="research", joint_kelly_enabled=True
    )
    assert [leg.decision.recommended_contracts for leg in off.legs] == [
        leg.decision.recommended_contracts for leg in on_no_ladder.legs
    ]
