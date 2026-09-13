"""Depth-aware two-level taker cross for the LIVE profile (2026-09-13).

What binds live size is not the position cap (largest live position in 13
days: $17.56 against a $30 cap) but ``_taker_cross_quote`` truncating every
immediate cross to the displayed best-ask size (44 of 46 live fills). These
tests pin the scaling change:

* quote logic -- level 2 is taken only when the displayed ask is below the
  request, a fresh ladder is on the decision, the after-fee LOWER-BOUND edge
  at the level-2 price still clears the floor, the notional floor holds, and
  the booked lower-bound expected profit does not fall; hard cap two levels;
  a missing / stale / malformed ladder is the historical single-level cross;
* the paper fill model -- an immediate level-2 fill consumes exactly
  level-1 + level-2 displayed size at the level-2 cost, never more;
* the scan wiring -- ladders are fetched pre-entry for depth-bound live legs
  only, inside the shared per-target budget, and never block a trade;
* a replay-style pass over a ladder captured from the public API.

The frozen ``StrategyConfig()`` keeps ``limit_taker_cross_max_levels == 1``
so historical fingerprints and the conservative baseline stay reproducible.
"""

from __future__ import annotations

import inspect
import json
import math
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from sfo_kalshi_quant._cli import scan as scan_module
from sfo_kalshi_quant.config import StrategyConfig, strategy_config_for_profile
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.execution import buy_limit_for_decision, with_buy_limit
from sfo_kalshi_quant.fees import quadratic_fee_average_per_contract
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.orderbook_capture import (
    parse_orderbook_response,
    side_ask_ladder,
)
from sfo_kalshi_quant.paper import PaperTrader, _clamp_to_displayed_ask
from sfo_kalshi_quant.portfolio import PortfolioLeg, PortfolioLimits, PortfolioPlan

# Captured 2026-09-13 from the public API:
#   GET /trade-api/v2/markets/KXHIGHNY-26SEP13-B80.5/orderbook?depth=3
# The market listing at the same instant read yes_bid 0.18 x 4, yes_ask
# 0.19 x 34, no_bid 0.81, no_ask 0.82 -- i.e. the best ``no_dollars`` entry
# IS the displayed YES ask and the best ``yes_dollars`` entry IS the displayed
# NO ask. Best price last, as the API returns it.
LADDER_FIXTURE = {
    "orderbook_fp": {
        "no_dollars": [["0.7900", "31.00"], ["0.8000", "110.70"], ["0.8100", "34.00"]],
        "yes_dollars": [["0.1600", "29.00"], ["0.1700", "31.00"], ["0.1800", "4.00"]],
    }
}
TICKER = "KXHIGHNY-26SEP14-B80.5"
TARGET_DATE = "2026-09-14"
NO_LADDER = ((0.82, 4.0), (0.83, 31.0))


def _live_config(**overrides) -> StrategyConfig:
    return StrategyConfig(**{**strategy_config_for_profile("live").__dict__, **overrides})


def _decision(**overrides) -> TradeDecision:
    values = {
        "ticker": TICKER,
        "label": "80° to 81°",
        "action": "BUY_NO",
        "approved": True,
        "probability": 0.95,
        "probability_lcb": 0.90,
        "yes_bid": 0.18,
        "yes_ask": 0.19,
        "spread": 0.01,
        "fee_per_contract": 0.0103,
        "cost_per_contract": 0.8303,
        "edge": 0.1197,
        "edge_lcb": 0.0697,
        "kelly_fraction": 0.05,
        "recommended_contracts": 30.0,
        "expected_profit": 3.59,
        "reasons": ["sleeve=no_core"],
        "side": "NO",
        "entry_bid": 0.81,
        "entry_ask": 0.82,
        "entry_bid_size": 34.0,
        "entry_ask_size": 4.0,
        "ask_levels": NO_LADDER,
    }
    values.update(overrides)
    return TradeDecision(**values)


def _taker_cost(price: float, contracts: float, config: StrategyConfig) -> float:
    return price + quadratic_fee_average_per_contract(
        price,
        contracts,
        maker=False,
        fee_multiplier=config.fee_multiplier,
        taker_rate=config.taker_fee_rate,
        maker_rate=config.maker_fee_rate,
        series_ticker=TICKER,
    )


# ---------------------------------------------------------------------------
# ladder normalization
# ---------------------------------------------------------------------------


def test_side_ask_ladder_is_the_complement_of_the_opposite_bids():
    depth = parse_orderbook_response(LADDER_FIXTURE)
    assert depth is not None
    # A YES buyer lifts NO bids; the best one matches the listed yes_ask 0.19 x 34.
    assert side_ask_ladder(depth, "YES") == ((0.19, 34.0), (0.2, 110.7))
    # A NO buyer lifts YES bids; the best one matches the listed no_ask 0.82 x 4.
    assert side_ask_ladder(depth, "NO") == NO_LADDER
    assert side_ask_ladder(depth, "no", levels=3) == (NO_LADDER + ((0.84, 29.0),))


def test_side_ask_ladder_sorts_and_drops_empty_levels():
    payload = {
        "orderbook_fp": {
            "yes_dollars": [["0.1800", "4.00"], ["0.1600", "29.00"], ["0.1700", "0.00"]],
            "no_dollars": [],
        }
    }
    depth = parse_orderbook_response(payload)
    assert depth is not None
    assert side_ask_ladder(depth, "NO") == ((0.82, 4.0), (0.84, 29.0))
    assert side_ask_ladder(depth, "YES") == ()


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_frozen_baseline_is_single_level_and_live_walks_two():
    assert StrategyConfig().limit_taker_cross_max_levels == 1
    assert strategy_config_for_profile("live").limit_taker_cross_max_levels == 2
    # Research never reaches the generic taker path; pinned explicitly.
    research = strategy_config_for_profile("research")
    assert research.limit_taker_cross_max_levels == 1
    assert research.limit_taker_cross_enabled is False


# ---------------------------------------------------------------------------
# quote logic
# ---------------------------------------------------------------------------


def test_level_two_clears_the_floor_and_takes_ladder_depth():
    config = _live_config()
    quote = buy_limit_for_decision(_decision(), config)
    assert quote is not None
    assert quote.would_cross is True
    assert quote.levels_used == 2
    assert quote.price == 0.83
    # min(recommended 30, 4 + 31) whole contracts, all booked at level 2.
    assert quote.contracts == 30.0
    assert quote.displayed_depth == 35.0
    assert math.isclose(quote.cost_per_contract, _taker_cost(0.83, 30.0, config), abs_tol=1e-9)
    assert math.isclose(quote.edge_lcb, 0.90 - quote.cost_per_contract, abs_tol=1e-9)
    assert quote.edge_lcb >= config.limit_taker_cross_min_edge_lcb


def test_level_two_is_capped_at_two_levels_and_ladder_depth():
    config = _live_config()
    three_levels = NO_LADDER + ((0.84, 29.0),)
    quote = buy_limit_for_decision(
        _decision(recommended_contracts=60.0, ask_levels=three_levels), config
    )
    assert quote is not None
    assert quote.levels_used == 2
    assert quote.price == 0.83  # never the third level
    assert quote.contracts == 35.0  # 4 + 31, not 4 + 31 + 29


def test_level_two_falls_back_when_the_floor_fails_one_tick_worse():
    config = _live_config()
    # LCB clears the 0.82 taker cost (edge ~+0.0045) but not the 0.83 one.
    quote = buy_limit_for_decision(_decision(probability_lcb=0.835), config)
    assert quote is not None
    assert quote.would_cross is True
    assert quote.levels_used == 1
    assert quote.price == 0.82
    assert quote.contracts == 4.0
    assert quote.displayed_depth == 4.0


def test_missing_ladder_is_exactly_the_historical_single_level_cross():
    config = _live_config()
    without = buy_limit_for_decision(_decision(ask_levels=None), config)
    assert without is not None
    assert without.levels_used == 1
    assert without.price == 0.82
    assert without.contracts == 4.0
    empty = buy_limit_for_decision(_decision(ask_levels=()), config)
    one_level = buy_limit_for_decision(_decision(ask_levels=((0.82, 4.0),)), config)
    assert empty == without
    assert one_level == without


def test_stale_or_malformed_ladder_falls_back_to_level_one():
    config = _live_config()
    baseline = buy_limit_for_decision(_decision(ask_levels=None), config)
    # Best ladder offer is not the displayed ask the candidate was priced on.
    stale = buy_limit_for_decision(_decision(ask_levels=((0.83, 10.0), (0.84, 30.0))), config)
    assert stale == baseline
    # Non-increasing second level.
    flat = buy_limit_for_decision(_decision(ask_levels=((0.82, 4.0), (0.82, 30.0))), config)
    assert flat == baseline
    # Garbage entries.
    garbage = buy_limit_for_decision(_decision(ask_levels=((0.82, 4.0), ("x", None))), config)
    assert garbage == baseline
    nan = buy_limit_for_decision(
        _decision(ask_levels=((0.82, 4.0), (float("nan"), 30.0))), config
    )
    assert nan == baseline


def test_no_walk_when_the_displayed_ask_covers_the_request():
    config = _live_config()
    quote = buy_limit_for_decision(_decision(recommended_contracts=4.0), config)
    assert quote is not None
    assert quote.levels_used == 1
    assert quote.price == 0.82
    assert quote.contracts == 4.0


def test_walk_requires_strictly_more_contracts_and_no_lower_booked_lcb_profit():
    config = _live_config()
    # Two more contracts do not pay for a tick on the twelve already displayed.
    quote = buy_limit_for_decision(
        _decision(
            probability_lcb=0.86,
            recommended_contracts=14.0,
            entry_ask_size=12.0,
            ask_levels=((0.82, 12.0), (0.83, 2.0)),
        ),
        config,
    )
    assert quote is not None
    assert quote.levels_used == 1
    assert quote.contracts == 12.0
    level_one_ev = 12.0 * (0.86 - _taker_cost(0.82, 12.0, config))
    level_two_ev = 14.0 * (0.86 - _taker_cost(0.83, 14.0, config))
    assert level_two_ev < level_one_ev
    # Same ladder, wider edge: the walk now pays.
    quote = buy_limit_for_decision(
        _decision(
            probability_lcb=0.95,
            recommended_contracts=14.0,
            entry_ask_size=12.0,
            ask_levels=((0.82, 12.0), (0.83, 2.0)),
        ),
        config,
    )
    assert quote is not None
    assert quote.levels_used == 2
    assert quote.contracts == 14.0


def test_frozen_baseline_ignores_an_attached_ladder():
    config = StrategyConfig(
        limit_taker_cross_enabled=True,
        limit_taker_cross_min_edge_lcb=0.0,
        limit_taker_cross_min_notional=1.0,
    )
    assert config.limit_taker_cross_max_levels == 1
    quote = buy_limit_for_decision(_decision(), config)
    assert quote is not None
    assert quote.levels_used == 1
    assert quote.price == 0.82
    assert quote.contracts == 4.0


def test_notional_floor_is_judged_on_the_whole_two_level_order():
    config = _live_config()
    assert config.limit_taker_cross_min_notional == 1.0
    # One displayed contract (~$0.83) fails the $1 floor on its own and, on a
    # two-tick spread, rests at bid+1; with level 2 the order is five
    # contracts and executable.
    quote = buy_limit_for_decision(
        _decision(
            recommended_contracts=5.0,
            entry_bid=0.80,
            entry_ask_size=1.0,
            ask_levels=((0.82, 1.0), (0.83, 10.0)),
        ),
        config,
    )
    assert quote is not None
    assert quote.would_cross is True
    assert quote.levels_used == 2
    assert quote.contracts == 5.0
    resting = buy_limit_for_decision(
        _decision(
            recommended_contracts=5.0, entry_bid=0.80, entry_ask_size=1.0, ask_levels=None
        ),
        config,
    )
    assert resting is not None
    assert resting.would_cross is False
    assert resting.price == 0.81


def test_with_buy_limit_marks_the_level_and_the_ladder_used():
    config = _live_config()
    decided = with_buy_limit(_decision(), config)
    assert decided.taker_levels_used == 2
    assert decided.limit_price == 0.83
    assert decided.recommended_contracts == 30.0
    assert decided.binding_constraint is None
    assert any(
        reason.startswith("execution: two-level taker cross 0.82x4 + 0.83x31 -> 30")
        for reason in decided.reasons
    )
    # Re-quoting (the account-policy fit does this) must not stack markers.
    again = with_buy_limit(replace(decided, recommended_contracts=20.0), config)
    two_level = [r for r in again.reasons if r.startswith("execution: two-level")]
    assert len(two_level) == 1
    assert "-> 20 contracts booked at 0.83" in two_level[0]
    # Depth-clamped request is still surfaced as such.
    clamped = with_buy_limit(_decision(recommended_contracts=60.0), config)
    assert clamped.recommended_contracts == 35.0
    assert clamped.binding_constraint == "visible_ask_depth"
    assert clamped.taker_levels_used == 2
    # A single-level cross carries level 1; a rest carries None.
    single = with_buy_limit(_decision(ask_levels=None), config)
    assert single.taker_levels_used == 1
    rest = with_buy_limit(_decision(probability_lcb=0.80, ask_levels=None), config)
    assert rest.taker_levels_used is None


# ---------------------------------------------------------------------------
# paper fill model
# ---------------------------------------------------------------------------


def test_clamp_uses_the_quoted_ladder_depth_only_when_given():
    decision = _decision(recommended_contracts=60.0)
    two_level = _clamp_to_displayed_ask(decision, displayed_depth=35.0)
    assert two_level is not None
    assert two_level.recommended_contracts == 35.0
    single = _clamp_to_displayed_ask(decision)
    assert single is not None
    assert single.recommended_contracts == 4.0
    assert _clamp_to_displayed_ask(decision, displayed_depth=0.0) is None


def _live_trader(tmp: str) -> tuple[PaperTrader, PaperStore]:
    store = PaperStore(Path(tmp) / "paper.db")
    trader = PaperTrader(
        store,
        strategy_config_for_profile("live"),
        risk_profile="live",
        entry_mode="limit",
    )
    return trader, store


def _order_row(store: PaperStore, order_id: int) -> sqlite3.Row:
    with store.connect() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM paper_orders WHERE id=?", (order_id,)).fetchone()


def test_paper_fill_consumes_level_one_plus_level_two_at_the_level_two_cost():
    with TemporaryDirectory() as tmp:
        trader, store = _live_trader(tmp)
        order_ids = trader.place_approved(TARGET_DATE, [_decision()], bankroll=1000.0)
        assert len(order_ids) == 1
        row = _order_row(store, order_ids[0])

    assert row["status"] == "PAPER_FILLED"
    assert row["fill_model"] == "immediate_visible_quote"
    assert row["entry_mode"] == "limit"
    assert row["contracts"] == 30.0
    assert row["filled_contracts"] == 30.0
    assert row["remaining_contracts"] == 0.0
    assert row["entry_price"] == 0.83
    assert row["limit_price"] == 0.83
    # The whole order is booked at the level-2 cost.
    expected_cost = _taker_cost(0.83, 30.0, strategy_config_for_profile("live"))
    assert math.isclose(row["cost_per_contract"], expected_cost, abs_tol=1e-9)
    # The listing's displayed best-ask size is preserved on the row ...
    assert row["entry_ask_size"] == 4.0
    # ... and the ladder the quote walked is in both the quote snapshot and
    # the entry diagnostics, for the markout study.
    quote = json.loads(row["quote_snapshot_json"])
    assert quote["taker_levels_used"] == 2
    assert quote["ask_levels"] == [[0.82, 4.0], [0.83, 31.0]]
    assert quote["limit_price"] == 0.83
    assert quote["ask"] == 0.82
    signal = json.loads(row["diagnostics_json"])["signal"]
    assert signal["taker_levels_used"] == 2
    assert signal["ask_levels"] == [[0.82, 4.0], [0.83, 31.0]]
    assert signal["entry_ask"] == 0.82
    assert any("two-level taker cross" in r for r in json.loads(row["reasons_json"]))


def test_paper_fill_never_invents_depth_beyond_the_two_ladder_levels():
    with TemporaryDirectory() as tmp:
        trader, store = _live_trader(tmp)
        order_ids = trader.place_approved(
            TARGET_DATE,
            [_decision(recommended_contracts=60.0, ask_levels=NO_LADDER + ((0.84, 29.0),))],
            bankroll=1000.0,
        )
        assert len(order_ids) == 1
        row = _order_row(store, order_ids[0])
    assert row["status"] == "PAPER_FILLED"
    assert row["contracts"] == 35.0  # 4 + 31 -- the third level is never touched
    assert row["filled_contracts"] == 35.0
    assert row["entry_price"] == 0.83


def test_normal_position_cap_still_binds_a_two_level_order():
    with TemporaryDirectory() as tmp:
        trader, store = _live_trader(tmp)
        order_ids = trader.place_approved(
            TARGET_DATE,
            [_decision(recommended_contracts=60.0, ask_levels=((0.82, 4.0), (0.83, 100.0)))],
            bankroll=1000.0,
        )
        assert len(order_ids) == 1
        row = _order_row(store, order_ids[0])
    spend = row["contracts"] * row["cost_per_contract"]
    assert spend <= 30.0 + 1e-9  # NORMAL_POSITION_CAP
    assert spend > 25.0  # and the cap, not the ladder, is what bound it
    assert row["contracts"] < 60.0
    assert row["entry_price"] == 0.83
    assert json.loads(row["quote_snapshot_json"])["taker_levels_used"] == 2


def test_paper_fill_without_a_ladder_is_the_historical_single_level_fill():
    with TemporaryDirectory() as tmp:
        trader, store = _live_trader(tmp)
        order_ids = trader.place_approved(TARGET_DATE, [_decision(ask_levels=None)], bankroll=1000.0)
        assert len(order_ids) == 1
        row = _order_row(store, order_ids[0])
    assert row["contracts"] == 4.0
    assert row["entry_price"] == 0.82
    quote = json.loads(row["quote_snapshot_json"])
    assert quote["taker_levels_used"] == 1
    assert "ask_levels" not in quote


# ---------------------------------------------------------------------------
# scan wiring
# ---------------------------------------------------------------------------


def _leg(decision: TradeDecision, sleeve: str = "no_core") -> PortfolioLeg:
    return PortfolioLeg(
        sleeve=sleeve,
        decision=decision,
        spend=float(decision.recommended_contracts) * decision.cost_per_contract,
        expected_profit=decision.expected_profit,
        growth_score=0.1,
    )


def _plan(legs: list[PortfolioLeg]) -> PortfolioPlan:
    return PortfolioPlan(
        run_id="PF-two-level",
        risk_profile="live",
        approved=True,
        legs=legs,
        arbitrage_opportunities=[],
        total_spend=sum(leg.spend for leg in legs),
        worst_case_loss=0.0,
        expected_profit=0.0,
        reasons=[],
        limits=PortfolioLimits(
            risk_profile="live",
            bankroll=1000.0,
            max_daily_loss=80.0,
            yes_sleeve=16.0,
            explore_sleeve=0.0,
        ),
    )


def _attach(plan, *, config=None, client, trader=None, deadline=None):
    store = Mock()
    trader = trader or PaperTrader(
        Mock(), strategy_config_for_profile("live"), risk_profile="live", entry_mode="limit"
    )
    result = scan_module._attach_pre_entry_ask_ladders(
        plan,
        config or strategy_config_for_profile("live"),
        client,
        trader,
        target_date=TARGET_DATE,
        store=store,
        risk_profile="live",
        deadline=time.monotonic() + 10.0 if deadline is None else deadline,
    )
    return result, store


def test_attach_threads_the_side_ladder_onto_depth_bound_directional_legs_only():
    client = Mock()
    client.get_orderbook.return_value = LADDER_FIXTURE
    depth_bound = _decision(ask_levels=None)  # 4 displayed < 30 requested
    covered = _decision(ask_levels=None, recommended_contracts=3.0, side="YES")
    box = _decision(ask_levels=None, ticker="KXHIGHNY-26SEP14-B78.5")
    plan = _plan([_leg(depth_bound), _leg(covered), _leg(box, sleeve="arbitrage")])

    (attached, captured), store = _attach(plan, client=client)

    client.get_orderbook.assert_called_once_with(TICKER, depth=3)
    assert captured == {TICKER}
    assert attached.legs[0].decision.ask_levels == NO_LADDER
    assert attached.legs[1] is plan.legs[1]  # request covered by the displayed ask
    assert attached.legs[2] is plan.legs[2]  # arbitrage legs are never walked
    # The fetched book is recorded as depth telemetry so the post-placement
    # capture (which is told to skip this ticker) does not spend the budget twice.
    store.record_orderbook_depth.assert_called_once()
    recorded = store.record_orderbook_depth.call_args.kwargs
    assert recorded["market_ticker"] == TICKER
    assert recorded["risk_profile"] == "live"
    assert recorded["no_levels"] == [[0.79, 31.0], [0.8, 110.7], [0.81, 34.0]]
    # Both sides of one market share the single fetch, each in its own terms.
    yes_leg = _decision(ask_levels=None, side="YES", entry_ask=0.19, entry_ask_size=34.0, recommended_contracts=90.0)
    (attached, _), _ = _attach(_plan([_leg(depth_bound), _leg(yes_leg)]), client=client)
    assert attached.legs[0].decision.ask_levels == NO_LADDER
    assert attached.legs[1].decision.ask_levels == ((0.19, 34.0), (0.2, 110.7))


def test_attach_is_a_no_op_without_a_client_in_market_mode_or_for_research():
    plan = _plan([_leg(_decision(ask_levels=None))])
    (same, captured), store = _attach(plan, client=None)
    assert same is plan and captured == set()

    client = Mock()
    client.get_orderbook.return_value = LADDER_FIXTURE
    market_trader = PaperTrader(
        Mock(), strategy_config_for_profile("live"), risk_profile="live", entry_mode="market"
    )
    (same, captured), _ = _attach(plan, client=client, trader=market_trader)
    assert same is plan and captured == set()
    client.get_orderbook.assert_not_called()

    (same, captured), _ = _attach(
        plan, client=client, config=strategy_config_for_profile("research")
    )
    assert same is plan and captured == set()
    client.get_orderbook.assert_not_called()

    frozen = StrategyConfig(limit_taker_cross_enabled=True)
    (same, captured), _ = _attach(plan, client=client, config=frozen)
    assert same is plan and captured == set()
    client.get_orderbook.assert_not_called()


def test_attach_never_blocks_a_trade_on_the_ladder():
    plan = _plan([_leg(_decision(ask_levels=None))])
    # Fetch failure: the leg is untouched and the single-level cross applies.
    failing = Mock()
    failing.get_orderbook.side_effect = OSError("network down")
    (attached, captured), store = _attach(plan, client=failing)
    assert attached.legs[0] is plan.legs[0]
    assert captured == set()
    store.record_orderbook_depth.assert_not_called()
    # Malformed book: same.
    malformed = Mock()
    malformed.get_orderbook.return_value = {"orderbook_fp": "nope"}
    (attached, captured), _ = _attach(plan, client=malformed)
    assert attached.legs[0] is plan.legs[0]
    assert captured == set()
    # Budget exhausted before the first fetch: no API call at all.
    idle = Mock()
    idle.get_orderbook.return_value = LADDER_FIXTURE
    (attached, captured), _ = _attach(plan, client=idle, deadline=time.monotonic() - 1.0)
    idle.get_orderbook.assert_not_called()
    assert attached.legs[0] is plan.legs[0]
    # Telemetry write failure never propagates.
    client = Mock()
    client.get_orderbook.return_value = LADDER_FIXTURE
    store = Mock()
    store.record_orderbook_depth.side_effect = RuntimeError("disk full")
    attached, captured = scan_module._attach_pre_entry_ask_ladders(
        plan,
        strategy_config_for_profile("live"),
        client,
        PaperTrader(Mock(), strategy_config_for_profile("live"), risk_profile="live", entry_mode="limit"),
        target_date=TARGET_DATE,
        store=store,
        risk_profile="live",
        deadline=time.monotonic() + 10.0,
    )
    assert attached.legs[0].decision.ask_levels == NO_LADDER
    assert captured == {TICKER}


def test_portfolio_scan_fetches_ladders_before_recording_inside_the_shared_budget():
    source = inspect.getsource(scan_module._portfolio_scan_one_target)
    attach_at = source.index("_attach_pre_entry_ask_ladders(")
    restate_at = source.index("_restate_recorded_execution(")
    record_at = source.index("store.record_decisions(")
    place_at = source.index("_place_portfolio_orders(")
    telemetry_at = source.index("_capture_orderbook_depth_for_legs(")
    assert attach_at < restate_at < record_at < place_at < telemetry_at
    # One per-target budget for both the pre-entry fetch and the telemetry.
    assert "ladder_deadline = time.monotonic() + _ORDERBOOK_CAPTURE_BUDGET_SECONDS" in source
    assert "budget_seconds=max(0.0, ladder_deadline - time.monotonic())" in source
    assert "skip_tickers=prefetched_tickers" in source
    # The research branch returns before any of this.
    assert source.index('if risk_profile == "research":') < attach_at


def test_telemetry_capture_skips_tickers_fetched_pre_entry():
    client = Mock()
    client.get_orderbook.return_value = LADDER_FIXTURE
    store = Mock()
    legs = [_leg(_decision(ask_levels=None)), _leg(_decision(ask_levels=None, ticker="KXHIGHNY-26SEP14-B78.5"))]
    scan_module._capture_orderbook_depth_for_legs(
        strategy_config_for_profile("live"),
        client,
        legs,
        target_date=TARGET_DATE,
        scan_run_id=None,
        store=store,
        risk_profile="live",
        skip_tickers={TICKER},
    )
    client.get_orderbook.assert_called_once_with("KXHIGHNY-26SEP14-B78.5", depth=3)


# ---------------------------------------------------------------------------
# replay-style pass over the captured ladder
# ---------------------------------------------------------------------------


def test_replay_of_the_captured_ladder_end_to_end():
    """Listing -> ladder -> quote -> paper fill, all from one captured book."""

    depth = parse_orderbook_response(LADDER_FIXTURE)
    assert depth is not None
    ladder = side_ask_ladder(depth, "NO", levels=2)
    # The listing at capture priced the NO side at 0.82 x 4; a 30-contract
    # request against that book was a 4-contract cross before this change.
    listing = _decision(ask_levels=None)
    assert ladder[0] == (listing.entry_ask, listing.entry_ask_size)
    before = buy_limit_for_decision(listing, strategy_config_for_profile("live"))
    assert before is not None and before.contracts == 4.0 and before.price == 0.82

    with TemporaryDirectory() as tmp:
        trader, store = _live_trader(tmp)
        placed = trader.place_approved(
            TARGET_DATE, [replace(listing, ask_levels=ladder)], bankroll=1000.0
        )
        assert len(placed) == 1
        row = _order_row(store, placed[0])
        # A second scan tick sees the open position and does not re-enter
        # (single entry slot per market/side is unchanged).
        assert trader.place_approved(
            TARGET_DATE, [replace(listing, ask_levels=ladder)], bankroll=1000.0
        ) == []

    assert row["contracts"] == 30.0
    assert row["entry_price"] == 0.83
    assert row["status"] == "PAPER_FILLED"
    booked = json.loads(row["quote_snapshot_json"])
    assert booked["ask_levels"] == [list(level) for level in ladder]
    assert booked["taker_levels_used"] == 2
    # Median live entry was $2.82; this book books 30 x ~0.84.
    assert 24.0 < row["contracts"] * row["cost_per_contract"] < 26.0
