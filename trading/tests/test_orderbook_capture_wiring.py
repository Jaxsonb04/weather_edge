"""Wiring: orderbook-depth capture inside the research scan path.

Complements test_orderbook_capture.py (which pins the pure parser and the
never-raises fetch wrapper) by proving the capture is correctly SCOPED and
SEQUENCED inside _execute_research_scan_context:

* it only fires when the flag is on AND a client is supplied;
* it targets the target sleeve's gated legs (the allocator's actual
  candidates), deduplicated by ticker;
* it runs strictly after order placement, so a capture failure cannot
  affect admit_target_orders / the returned execution result;
* an unexpected ``plans`` shape (a bare object, as an existing sibling test
  in this file already constructs) degrades silently rather than raising.
"""

import contextlib
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sfo_kalshi_quant._cli import scan as scan_module
from sfo_kalshi_quant.config import StrategyConfig, strategy_config_for_profile
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.portfolio import PortfolioLeg, PortfolioLimits, PortfolioPlan


def _decision(ticker: str) -> TradeDecision:
    return TradeDecision(
        ticker=ticker,
        label="80 to 81",
        action="BUY_NO",
        approved=True,
        probability=0.9,
        probability_lcb=0.85,
        yes_bid=0.08,
        yes_ask=0.10,
        spread=0.02,
        fee_per_contract=0.01,
        cost_per_contract=0.91,
        edge=0.04,
        edge_lcb=0.0,
        kelly_fraction=0.02,
        recommended_contracts=5.0,
        expected_profit=0.2,
        reasons=[],
        side="NO",
    )


def _leg(ticker: str) -> PortfolioLeg:
    return PortfolioLeg(
        sleeve="target",
        decision=_decision(ticker),
        spend=4.5,
        expected_profit=0.2,
        growth_score=0.1,
    )


def _plan(legs: list[PortfolioLeg]) -> PortfolioPlan:
    return PortfolioPlan(
        run_id="r",
        risk_profile="research",
        approved=True,
        legs=legs,
        arbitrage_opportunities=[],
        total_spend=sum(leg.spend for leg in legs),
        worst_case_loss=0.0,
        expected_profit=0.0,
        reasons=[],
        limits=PortfolioLimits(
            risk_profile="research",
            bankroll=1000.0,
            max_daily_loss=120.0,
            yes_sleeve=40.0,
            explore_sleeve=40.0,
        ),
    )


def _base_context():
    return SimpleNamespace(
        decisions=[object()],
        series_ticker="KXHIGHTSFO",
        intraday=None,
        forecast=object(),
        event=object(),
        consensus=object(),
    )


def _run_scan_context(
    config, *, kalshi_client, plans, target_decisions=None, execution_config=None
):
    """Drive _execute_research_scan_context.

    NOTE: the function under test always rebuilds its own execution config via
    ``strategy_config_for_profile("research")`` internally -- the ``config``
    parameter passed to it is not what determines capture behavior (this
    matches the function's real, pre-existing design: it is only ever called
    from the research scan path, which is not configurable per-call). Pass
    ``execution_config`` to control what that internal call returns.
    """

    context = _base_context()
    store = Mock()
    store.research_objective_day.return_value = date(2026, 7, 18)
    store.research_account_state.return_value = {"available_cash": 900.0}
    store.research_realized_pnl_for_day.return_value = 0.0
    trader = Mock()
    trader.execute_research_plans.return_value = object()

    patches = [
        patch.object(
            scan_module,
            "prepare_research_target_decisions",
            return_value=target_decisions or [],
        ),
        patch.object(scan_module, "ResearchOpportunity", side_effect=lambda d, t, l: (d, t, l)),
        patch.object(scan_module, "allocate_research_plans", return_value=plans),
        patch.object(scan_module, "PaperTrader", return_value=trader),
    ]
    if execution_config is not None:
        patches.append(
            patch.object(scan_module, "strategy_config_for_profile", return_value=execution_config)
        )

    with contextlib.ExitStack() as stack:
        for ctx in patches:
            stack.enter_context(ctx)
        result = scan_module._execute_research_scan_context(
            context,
            target=date(2026, 7, 19),
            store=store,
            config=config,
            entry_allowed=True,
            entry_block_reason=None,
            place_paper=False,
            place_research_target=True,
            place_research_motion=True,
            forecast_snapshot_id=10,
            market_snapshot_id=20,
            scan_run_id="scan-1",
            kalshi_client=kalshi_client,
        )
    return result, store, trader


def test_frozen_baseline_never_touches_the_client():
    """The function under test only ever runs the research profile in
    production, so exercise the actual gate: an execution config with the
    flag off (the frozen StrategyConfig() baseline) must never fetch."""

    baseline = StrategyConfig()
    assert baseline.orderbook_depth_capture_enabled is False
    client = Mock()
    plans = SimpleNamespace(target=_plan([_leg("KXHIGHTSFO-26JUL19-T74")]), motion=object())

    _run_scan_context(
        strategy_config_for_profile("research"),
        kalshi_client=client,
        plans=plans,
        execution_config=baseline,
    )

    client.get_orderbook.assert_not_called()


def test_capture_with_no_client_supplied_is_a_no_op():
    config = strategy_config_for_profile("research")
    assert config.orderbook_depth_capture_enabled is True
    plans = SimpleNamespace(target=_plan([_leg("KXHIGHTSFO-26JUL19-T74")]), motion=object())

    # Must not raise even though the flag is on, because no client was given.
    _run_scan_context(config, kalshi_client=None, plans=plans)


def test_enabled_capture_fetches_and_records_each_distinct_leg_ticker():
    config = strategy_config_for_profile("research")
    client = Mock()
    client.get_orderbook.return_value = {
        "orderbook_fp": {"yes_dollars": [["0.10", "5"]], "no_dollars": [["0.90", "5"]]}
    }
    legs = [
        _leg("KXHIGHTSFO-26JUL19-T74"),
        _leg("KXHIGHTSFO-26JUL19-T74"),  # duplicate ticker: dedup expected
        _leg("KXHIGHCHI-26JUL19-T90"),
    ]
    plans = SimpleNamespace(target=_plan(legs), motion=object())

    _, store, _ = _run_scan_context(config, kalshi_client=client, plans=plans)

    assert client.get_orderbook.call_count == 2
    fetched_tickers = {call.args[0] for call in client.get_orderbook.call_args_list}
    assert fetched_tickers == {"KXHIGHTSFO-26JUL19-T74", "KXHIGHCHI-26JUL19-T90"}
    assert store.record_orderbook_depth.call_count == 2
    recorded = store.record_orderbook_depth.call_args_list[0].kwargs
    assert recorded["risk_profile"] == "research"
    assert recorded["scan_run_id"] == "scan-1"
    assert recorded["yes_levels"] == [[0.10, 5.0]]
    assert recorded["no_levels"] == [[0.90, 5.0]]


def test_capture_uses_the_configured_level_count():
    seven_levels = StrategyConfig(
        **{
            **strategy_config_for_profile("research").__dict__,
            "orderbook_depth_capture_levels": 7,
        }
    )
    client = Mock()
    client.get_orderbook.return_value = {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}
    plans = SimpleNamespace(target=_plan([_leg("KXHIGHTSFO-26JUL19-T74")]), motion=object())

    _run_scan_context(
        strategy_config_for_profile("research"),
        kalshi_client=client,
        plans=plans,
        execution_config=seven_levels,
    )

    client.get_orderbook.assert_called_once_with("KXHIGHTSFO-26JUL19-T74", depth=7)


def test_client_failure_does_not_affect_placement_or_raise():
    config = strategy_config_for_profile("research")
    client = Mock()
    client.get_orderbook.side_effect = OSError("network down")
    plans = SimpleNamespace(target=_plan([_leg("KXHIGHTSFO-26JUL19-T74")]), motion=object())

    result, store, trader = _run_scan_context(config, kalshi_client=client, plans=plans)

    actual_plans, actual_execution, recorded = result
    assert actual_plans is plans
    assert actual_execution is trader.execute_research_plans.return_value
    store.record_orderbook_depth.assert_not_called()


def test_store_write_failure_does_not_raise_or_affect_the_result():
    config = strategy_config_for_profile("research")
    client = Mock()
    client.get_orderbook.return_value = {
        "orderbook_fp": {"yes_dollars": [["0.10", "5"]], "no_dollars": []}
    }
    plans = SimpleNamespace(target=_plan([_leg("KXHIGHTSFO-26JUL19-T74")]), motion=object())

    context = _base_context()
    store = Mock()
    store.research_objective_day.return_value = date(2026, 7, 18)
    store.research_account_state.return_value = {"available_cash": 900.0}
    store.research_realized_pnl_for_day.return_value = 0.0
    store.record_orderbook_depth.side_effect = RuntimeError("disk full")
    trader = Mock()
    trader.execute_research_plans.return_value = object()

    with (
        patch.object(scan_module, "prepare_research_target_decisions", return_value=[]),
        patch.object(scan_module, "ResearchOpportunity", side_effect=lambda d, t, l: (d, t, l)),
        patch.object(scan_module, "allocate_research_plans", return_value=plans),
        patch.object(scan_module, "PaperTrader", return_value=trader),
    ):
        actual_plans, actual_execution, recorded = scan_module._execute_research_scan_context(
            context,
            target=date(2026, 7, 19),
            store=store,
            config=config,
            entry_allowed=True,
            entry_block_reason=None,
            place_paper=False,
            place_research_target=True,
            place_research_motion=True,
            forecast_snapshot_id=10,
            market_snapshot_id=20,
            scan_run_id="scan-1",
            kalshi_client=client,
        )

    assert actual_plans is plans
    assert actual_execution is trader.execute_research_plans.return_value


def test_unexpected_plans_shape_degrades_silently():
    """Mirrors the sibling test in test_entry_target_gate.py, which uses a bare
    object() for plans.target. That must never raise even with a real client."""

    config = strategy_config_for_profile("research")
    client = Mock()
    plans = SimpleNamespace(target=object(), motion=object())

    # Must not raise.
    _run_scan_context(config, kalshi_client=client, plans=plans)
    client.get_orderbook.assert_not_called()
