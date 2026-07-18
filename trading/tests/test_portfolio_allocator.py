from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from sfo_kalshi_quant.arbitrage import build_arbitrage_opportunities
from sfo_kalshi_quant.config import strategy_config_for_profile
from sfo_kalshi_quant.models import MarketBin, TradeDecision
from sfo_kalshi_quant.portfolio import allocate_portfolio, portfolio_limits_for_profile
from sfo_kalshi_quant.portfolio import PortfolioLeg
from sfo_kalshi_quant.research_portfolio import (
    ResearchOpportunity,
    allocate_research_plans,
    city_target_worst_case_loss,
)


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


def test_target_allocates_every_positive_lcb_day_ahead_candidate_until_cap() -> None:
    opportunities = [
        ResearchOpportunity(
            decision=_decision(
                _market(
                    f"{70 + idx}° to {71 + idx}°",
                    ticker=f"KXHIGHTSFO-26JUL20-B{70.5 + idx}",
                    floor=70 + idx,
                    cap=71 + idx,
                    yes_ask=0.20,
                ),
                side="YES",
                spend=20.0,
                probability=0.45,
                edge=0.25,
                edge_lcb=0.05,
            ),
            target_date="2026-07-20",
            lead_days=2,
        )
        for idx in range(3)
    ]

    plans = allocate_research_plans(opportunities)

    assert [leg.decision.ticker for leg in plans.target.legs] == [
        opportunity.decision.ticker for opportunity in opportunities
    ]
    assert plans.target.total_spend == 60.0


def test_target_rejects_same_day_and_negative_lcb_without_loosening_gates() -> None:
    market = _market(
        "70° to 71°",
        ticker="KXHIGHTSFO-26JUL20-B70.5",
        floor=70,
        cap=71,
        yes_ask=0.20,
    )
    accepted = ResearchOpportunity(
        _decision(
            replace(market, ticker=f"{market.ticker}-OK"),
            side="YES",
            spend=20.0,
            probability=0.45,
            edge=0.25,
            edge_lcb=0.05,
        ),
        "2026-07-20",
        1,
    )
    same_day = replace(accepted, decision=replace(accepted.decision, ticker=f"{market.ticker}-SAME"), lead_days=0)
    negative_lcb = replace(
        accepted,
        decision=replace(accepted.decision, ticker=f"{market.ticker}-NEG", edge_lcb=-0.001),
    )

    plans = allocate_research_plans([same_day, negative_lcb, accepted], realized_today=-49.0)

    assert [leg.decision.ticker for leg in plans.target.legs] == [accepted.decision.ticker]
    assert {row.ticker: row.status for row in plans.target.dispositions} == {
        accepted.decision.ticker: "selected",
        negative_lcb.decision.ticker: "rejected",
        same_day.decision.ticker: "rejected",
    }


def test_motion_places_one_contract_for_every_eligible_candidate_deterministically() -> None:
    opportunities = []
    for idx, (edge, edge_lcb) in enumerate(((0.04, -0.06), (0.03, 0.01), (-0.01, 0.01), (0.02, -0.08))):
        market = _market(
            f"{70 + idx}° to {71 + idx}°",
            ticker=f"KXHIGHTSFO-26JUL18-B{70.5 + idx}",
            floor=70 + idx,
            cap=71 + idx,
            yes_ask=0.20,
        )
        opportunities.append(
            ResearchOpportunity(
                _decision(
                    market,
                    side="YES",
                    spend=10.0,
                    probability=0.40,
                    edge=edge,
                    edge_lcb=edge_lcb,
                ),
                "2026-07-18",
                0,
            )
        )

    forward = allocate_research_plans(opportunities, run_id="stable")
    reverse = allocate_research_plans(list(reversed(opportunities)), run_id="stable")

    expected = sorted([opportunities[0].decision.ticker, opportunities[1].decision.ticker])
    assert [leg.decision.ticker for leg in forward.motion.legs] == expected
    assert [leg.decision.ticker for leg in reverse.motion.legs] == expected
    assert all(leg.decision.recommended_contracts == 1 for leg in forward.motion.legs)
    assert len(forward.motion.dispositions) == len(opportunities)


def test_city_target_loss_models_mutually_exclusive_integer_settlement_bins() -> None:
    decisions = [
        _decision(
            _market(
                f"{floor}° to {floor + 1}°",
                ticker=f"KXHIGHTSFO-26JUL20-B{floor + 0.5}",
                floor=floor,
                cap=floor + 1,
                no_ask=0.80,
            ),
            side="NO",
            spend=20.0,
            probability=0.10,
            edge=0.10,
            edge_lcb=0.02,
        )
        for floor in (70, 72)
    ]
    legs = [
        PortfolioLeg("target", decision, 20.0, decision.expected_profit, 0.0)
        for decision in decisions
    ]

    # At most one bounded YES bracket can settle.  The losing NO leg's $20
    # loss is offset by the other NO leg's $5 settlement profit.
    assert city_target_worst_case_loss(legs, range(68, 76)) == 15.0


def test_pending_orders_reserve_their_full_possible_loss() -> None:
    decisions = [
        _decision(
            _market(
                f"{floor}° to {floor + 1}°",
                ticker=f"KXHIGHTSFO-26JUL20-B{floor + 0.5}",
                floor=floor,
                cap=floor + 1,
                no_ask=0.80,
            ),
            side="NO",
            spend=20.0,
            probability=0.10,
            edge=0.10,
            edge_lcb=0.02,
        )
        for floor in (70, 72)
    ]
    pending = [
        PortfolioLeg("target", decision, 20.0, decision.expected_profit, 0.0, pending=True)
        for decision in decisions
    ]

    assert city_target_worst_case_loss(pending, range(68, 76)) == 40.0


def test_target_enforces_city_and_correlated_region_scenario_caps() -> None:
    def pending_leg(series: str, target: str, spend: float) -> PortfolioLeg:
        decision = _decision(
            _market(
                "70° to 71°",
                ticker=f"{series}-26JUL20-B70.5",
                floor=70,
                cap=71,
                yes_ask=0.50,
            ),
            side="YES",
            spend=spend,
            probability=0.70,
            edge=0.20,
            edge_lcb=0.10,
        )
        return PortfolioLeg(
            "target",
            decision,
            spend,
            decision.expected_profit,
            0.0,
            target_date=target,
            pending=True,
        )

    active = [
        pending_leg("KXHIGHTSEA", "2026-07-20", 50.0),
        pending_leg("KXHIGHLAX", "2026-07-20", 50.0),
    ]
    candidates = [
        ResearchOpportunity(
            _decision(
                _market(
                    "72° to 73°",
                    ticker=f"{series}-26JUL20-B72.5",
                    floor=72,
                    cap=73,
                    yes_ask=0.50,
                ),
                side="YES",
                spend=30.0,
                probability=0.75,
                edge=0.25,
                edge_lcb=0.10,
            ),
            "2026-07-20",
            2,
        )
        for series in ("KXHIGHTSFO", "KXHIGHDEN")
    ]

    plans = allocate_research_plans(candidates, target_active_legs=active)

    assert [leg.decision.ticker for leg in plans.target.legs] == [candidates[1].decision.ticker]
    dispositions = {row.ticker: row for row in plans.target.dispositions}
    assert dispositions[candidates[0].decision.ticker].status == "capacity_blocked"
    assert "region-day" in (dispositions[candidates[0].decision.ticker].reason or "")


def test_target_enforces_city_target_cap_against_pending_full_loss() -> None:
    market = _market(
        "70° to 71°",
        ticker="KXHIGHTSFO-26JUL20-B70.5",
        floor=70,
        cap=71,
        yes_ask=0.50,
    )
    active_decision = _decision(
        market,
        side="YES",
        spend=50.0,
        probability=0.70,
        edge=0.20,
        edge_lcb=0.10,
    )
    active = PortfolioLeg(
        "target",
        active_decision,
        50.0,
        active_decision.expected_profit,
        0.0,
        target_date="2026-07-20",
        pending=True,
    )
    candidate = ResearchOpportunity(
        _decision(
            replace(market, ticker="KXHIGHTSFO-26JUL20-B72.5", floor_strike=72, cap_strike=73),
            side="YES",
            spend=20.0,
            probability=0.70,
            edge=0.20,
            edge_lcb=0.10,
        ),
        "2026-07-20",
        2,
    )

    plans = allocate_research_plans([candidate], target_active_legs=[active])

    assert plans.target.legs == []
    assert plans.target.dispositions[0].status == "capacity_blocked"
    assert "city-target" in (plans.target.dispositions[0].reason or "")


def test_partial_children_are_not_counted_as_separate_exposure_legs() -> None:
    decision = _decision(
        _market(
            "70° to 71°",
            ticker="KXHIGHTSFO-26JUL20-B70.5",
            floor=70,
            cap=71,
            yes_ask=0.20,
        ),
        side="YES",
        spend=20.0,
        probability=0.50,
        edge=0.30,
        edge_lcb=0.10,
    )
    root = PortfolioLeg(
        "target",
        decision,
        20.0,
        decision.expected_profit,
        0.0,
        pending=True,
        logical_position_id=42,
    )
    child = replace(root, is_partial_child=True)

    assert city_target_worst_case_loss([root, child], range(68, 74)) == 20.0


def test_target_position_risk_is_hard_capped_at_three_percent() -> None:
    candidate = ResearchOpportunity(
        _decision(
            _market(
                "70° to 71°",
                ticker="KXHIGHTSFO-26JUL20-B70.5",
                floor=70,
                cap=71,
                yes_ask=0.20,
            ),
            side="YES",
            spend=80.0,
            probability=0.50,
            edge=0.30,
            edge_lcb=0.10,
        ),
        "2026-07-20",
        2,
    )

    plans = allocate_research_plans([candidate])

    assert plans.target.legs[0].spend == 30.0
    assert plans.target.legs[0].decision.recommended_contracts == 150.0


def test_infeasible_fifty_dollar_report_never_loosens_target_gates_or_count() -> None:
    valid = ResearchOpportunity(
        _decision(
            _market(
                "70° to 71°",
                ticker="KXHIGHTSFO-26JUL20-B70.5",
                floor=70,
                cap=71,
                yes_ask=0.20,
            ),
            side="YES",
            spend=20.0,
            probability=0.30,
            edge=0.10,
            edge_lcb=0.01,
        ),
        "2026-07-20",
        2,
    )
    same_day = replace(valid, decision=replace(valid.decision, ticker=f"{valid.decision.ticker}-SAME"), lead_days=0)
    negative_lcb = replace(
        valid,
        decision=replace(valid.decision, ticker=f"{valid.decision.ticker}-NEG", edge_lcb=-0.001),
    )

    plans = allocate_research_plans([negative_lcb, same_day, valid], realized_today=-49.0)

    assert plans.target_pnl == 50.0
    assert plans.remaining_target == 99.0
    assert plans.available_conservative_expected_profit == 1.0
    assert plans.target_feasible_from_current_opportunity_set is False
    assert [leg.decision.ticker for leg in plans.target.legs] == [valid.decision.ticker]


def test_target_allocation_is_input_order_invariant_and_uses_conservative_priority() -> None:
    opportunities = [
        ResearchOpportunity(
            _decision(
                _market(
                    f"{70 + idx}° to {71 + idx}°",
                    ticker=f"KXHIGHDEN-26JUL2{idx}-B{70.5 + idx}",
                    floor=70 + idx,
                    cap=71 + idx,
                    yes_ask=cost,
                ),
                side="YES",
                spend=20.0,
                probability=0.50,
                edge=0.20,
                edge_lcb=lcb,
            ),
            f"2026-07-2{idx}",
            idx + 1,
        )
        for idx, (cost, lcb) in enumerate(((0.50, 0.05), (0.20, 0.04), (0.40, 0.06)))
    ]

    forward = allocate_research_plans(opportunities, run_id="invariant")
    reverse = allocate_research_plans(list(reversed(opportunities)), run_id="invariant")

    expected = [opportunities[1].decision.ticker, opportunities[2].decision.ticker, opportunities[0].decision.ticker]
    assert [leg.decision.ticker for leg in forward.target.legs] == expected
    assert [leg.decision.ticker for leg in reverse.target.legs] == expected


def test_target_enforces_aggregate_open_scenario_cap() -> None:
    active = []
    series = ("KXHIGHTSFO", "KXHIGHDEN", "KXHIGHMIA", "KXHIGHCHI")
    for idx in range(8):
        market = _market(
            "70° to 71°",
            ticker=f"{series[idx % len(series)]}-26JUL{10 + idx}-B70.5",
            floor=70,
            cap=71,
            yes_ask=0.50,
        )
        decision = _decision(
            market,
            side="YES",
            spend=30.0,
            probability=0.70,
            edge=0.20,
            edge_lcb=0.10,
        )
        active.append(
            PortfolioLeg(
                "target",
                decision,
                30.0,
                decision.expected_profit,
                0.0,
                target_date=f"2026-07-{10 + idx:02d}",
                pending=True,
            )
        )
    candidate = ResearchOpportunity(
        _decision(
            _market(
                "72° to 73°",
                ticker="KXHIGHDEN-26JUL30-B72.5",
                floor=72,
                cap=73,
                yes_ask=0.50,
            ),
            side="YES",
            spend=20.0,
            probability=0.70,
            edge=0.20,
            edge_lcb=0.10,
        ),
        "2026-07-30",
        2,
    )

    plans = allocate_research_plans([candidate], target_active_legs=active)

    assert plans.target.legs == []
    assert "aggregate" in (plans.target.dispositions[0].reason or "")


def test_motion_has_no_count_throttle_and_stops_only_at_scenario_cap() -> None:
    opportunities = []
    for idx in range(102):
        target = date(2026, 8, 1) + timedelta(days=idx)
        market = _market(
            "70° to 71°",
            ticker=f"KXHIGHDEN-26AUG{idx:03d}-B70.5",
            floor=70,
            cap=71,
            yes_ask=0.99,
        )
        opportunities.append(
            ResearchOpportunity(
                _decision(
                    market,
                    side="YES",
                    spend=9.90,
                    probability=0.995,
                    edge=0.005,
                    edge_lcb=-0.01,
                ),
                target.isoformat(),
                1,
            )
        )

    plans = allocate_research_plans(opportunities)

    assert len(plans.motion.legs) == 101
    assert all(leg.decision.recommended_contracts == 1 for leg in plans.motion.legs)
    blocked = [row for row in plans.motion.dispositions if row.status == "capacity_blocked"]
    assert len(blocked) == 1
    assert "aggregate" in (blocked[0].reason or "")
