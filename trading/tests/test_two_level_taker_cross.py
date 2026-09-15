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
* the fresh ladder, not the older listing size, decides the walk: when the
  fresh level-1 size covers the request the order crosses at level 1 for all
  of it;
* the scan wiring -- ladders are fetched pre-entry for depth-bound live legs
  only, as ONE attempt under a hard per-call deadline inside the per-target
  budget, and never block or delay a trade beyond that deadline;
* a replay-style pass over a ladder captured from the public API.

The frozen ``StrategyConfig()`` keeps ``limit_taker_cross_max_levels == 1``
so historical fingerprints and the conservative baseline stay reproducible.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import pytest

from sfo_kalshi_quant._cli import scan as scan_module
from sfo_kalshi_quant.cities import get_city
from sfo_kalshi_quant.config import StrategyConfig, strategy_config_for_profile
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.execution import buy_limit_for_decision, with_buy_limit
from sfo_kalshi_quant.fees import quadratic_fee_average_per_contract
from sfo_kalshi_quant.kalshi import KalshiPublicClient
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.orderbook_capture import (
    capture_orderbook_depth_within,
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


def test_fresh_level_one_covering_the_request_crosses_at_level_one_for_all_of_it():
    """Review 2026-09-13: the walk is keyed on the FRESH ladder, not the listing.

    The listing showed 12 contracts at the ask; the ladder fetched after it
    shows 34 there. A real order would fill all 30 at level 1, so nothing is
    booked a tick worse -- and on a thin edge the lower-bound EV guard no
    longer shrinks the order to the listing's 12 either.
    """

    config = _live_config()
    thick = ((0.82, 34.0), (0.83, 110.0))
    quote = buy_limit_for_decision(_decision(entry_ask_size=12.0, ask_levels=thick), config)
    assert quote is not None
    assert quote.would_cross is True
    assert (quote.levels_used, quote.price, quote.contracts) == (1, 0.82, 30.0)
    assert quote.displayed_depth == 34.0
    assert math.isclose(
        quote.cost_per_contract, _taker_cost(0.82, 30.0, config), abs_tol=1e-9
    )
    thin_edge = buy_limit_for_decision(
        _decision(entry_ask_size=12.0, probability_lcb=0.842, ask_levels=thick), config
    )
    assert thin_edge is not None
    assert (thin_edge.levels_used, thin_edge.price, thin_edge.contracts) == (1, 0.82, 30.0)
    decided = with_buy_limit(_decision(entry_ask_size=12.0, ask_levels=thick), config)
    assert decided.taker_levels_used == 1
    assert decided.limit_price == 0.82
    assert decided.recommended_contracts == 30.0
    assert not any(r.startswith("execution: two-level") for r in decided.reasons)


def test_level_one_is_sized_on_the_fresh_ladder_when_the_book_thinned():
    config = _live_config()
    # Level 2 fails the floor one tick worse, so the order is level 1 -- for
    # the 5 contracts the fresh book shows, not the 12 the older listing did.
    thinned = buy_limit_for_decision(
        _decision(
            entry_ask_size=12.0,
            probability_lcb=0.835,
            ask_levels=((0.82, 5.0), (0.83, 31.0)),
        ),
        config,
    )
    assert thinned is not None
    assert (
        thinned.levels_used,
        thinned.price,
        thinned.contracts,
        thinned.displayed_depth,
    ) == (1, 0.82, 5.0, 5.0)
    # A ladder whose best offer is not the displayed ask is ignored entirely:
    # the listing size applies, exactly as with no ladder at all.
    stale = buy_limit_for_decision(
        _decision(
            entry_ask_size=12.0,
            probability_lcb=0.835,
            ask_levels=((0.83, 50.0), (0.84, 50.0)),
        ),
        config,
    )
    without = buy_limit_for_decision(
        _decision(entry_ask_size=12.0, probability_lcb=0.835, ask_levels=None), config
    )
    assert without is not None
    assert stale == without
    assert (
        without.levels_used,
        without.price,
        without.contracts,
        without.displayed_depth,
    ) == (1, 0.82, 12.0, 12.0)


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


def test_journal_records_the_level_the_account_fit_places_not_the_pre_fit_level():
    """Release review 2026-09-13 (cross-track MEDIUM): TC-15 x two-level cross x account fit.

    A 60-contract request walks a fresh ladder to level 2 (0.83). The live
    position cap shrinks it to 35 contracts, which the 40-deep level 1 covers,
    so placement re-quotes at level 1 (0.82). A journal restated through
    ``with_entry_mode`` alone kept level 2, 60 contracts and 0.83.
    """

    decision = _decision(
        recommended_contracts=60.0, ask_levels=((0.82, 40.0), (0.83, 100.0))
    )
    with TemporaryDirectory() as tmp:
        trader, store = _live_trader(tmp)
        plan = _plan([_leg(decision)])
        (pre_fit,) = scan_module._restate_recorded_execution([decision], plan, trader)
        (journal,) = scan_module._restate_recorded_execution(
            [decision], plan, trader, target_date=TARGET_DATE, bankroll=1000.0
        )
        order_ids = trader.place_approved(TARGET_DATE, [decision], bankroll=1000.0)
        assert len(order_ids) == 1
        row = _order_row(store, order_ids[0])

    # Premise: without the account fit the restatement walks to level 2.
    assert (pre_fit.taker_levels_used, pre_fit.limit_price, pre_fit.recommended_contracts) == (
        2,
        0.83,
        60.0,
    )
    quote = json.loads(row["quote_snapshot_json"])
    assert (quote["taker_levels_used"], row["limit_price"], row["contracts"]) == (1, 0.82, 35.0)
    assert journal.approved is True
    assert journal.taker_levels_used == quote["taker_levels_used"]
    assert journal.limit_price == row["limit_price"]
    assert journal.recommended_contracts == row["contracts"]
    assert math.isclose(journal.limit_cost_per_contract, row["cost_per_contract"], abs_tol=1e-9)


def test_journal_restatement_keeps_the_entry_mode_quote_for_an_entry_placement_skips():
    decision = _decision(
        recommended_contracts=60.0, ask_levels=((0.82, 40.0), (0.83, 100.0))
    )
    with TemporaryDirectory() as tmp:
        trader, _store = _live_trader(tmp)
        plan = _plan([_leg(decision)])
        assert trader.place_approved(TARGET_DATE, [decision], bankroll=1000.0)
        (journal,) = scan_module._restate_recorded_execution(
            [decision], plan, trader, target_date=TARGET_DATE, bankroll=1000.0
        )
        (plain,) = trader.with_entry_mode([decision])

    # The market already holds a position, so placement would skip it; the
    # journal keeps the historical entry-mode quote instead of inventing terms.
    assert journal == plain


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


def test_paper_fill_books_a_fresh_level_one_order_at_level_one():
    thick = ((0.82, 34.0), (0.83, 110.0))
    with TemporaryDirectory() as tmp:
        trader, store = _live_trader(tmp)
        order_ids = trader.place_approved(
            TARGET_DATE,
            [_decision(entry_ask_size=12.0, ask_levels=thick)],
            bankroll=1000.0,
        )
        assert len(order_ids) == 1
        row = _order_row(store, order_ids[0])
    assert row["status"] == "PAPER_FILLED"
    assert (row["contracts"], row["filled_contracts"], row["entry_price"]) == (
        30.0,
        30.0,
        0.82,
    )
    expected_cost = _taker_cost(0.82, 30.0, strategy_config_for_profile("live"))
    assert math.isclose(row["cost_per_contract"], expected_cost, abs_tol=1e-9)
    # The older listing size stays on the row; the fresh ladder that sized
    # the fill is in the quote snapshot restatement verifies against.
    assert row["entry_ask_size"] == 12.0
    quote = json.loads(row["quote_snapshot_json"])
    assert quote["taker_levels_used"] == 1
    assert quote["ask_levels"] == [[0.82, 34.0], [0.83, 110.0]]


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
    # Attempted, so the post-placement telemetry does not re-hit the endpoint.
    assert captured == {TICKER}
    store.record_orderbook_depth.assert_not_called()
    # Malformed book: same.
    malformed = Mock()
    malformed.get_orderbook.return_value = {"orderbook_fp": "nope"}
    (attached, captured), _ = _attach(plan, client=malformed)
    assert attached.legs[0] is plan.legs[0]
    assert captured == {TICKER}
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


def _run_portfolio_scan(
    store: PaperStore,
    client,
    decisions: list[TradeDecision],
    *,
    calls: list,
    record_delay: float = 0.0,
    real_telemetry: bool = True,
) -> None:
    """Execute the REAL _portfolio_scan_one_target around a live plan.

    Context building, allocation, the entry gate and operator output are
    stubbed. The ladder attach, the recording restatement, placement (a real
    PaperTrader on a real PaperStore) and the post-placement telemetry run
    for real behind spies that record the order and time each is reached;
    ``store.record_decisions`` is a spy that can simulate a slow DB write.
    """

    plan = _plan([_leg(decision) for decision in decisions])
    city = get_city("nyc")
    context = SimpleNamespace(
        city=city,
        series_ticker=city.series_ticker,
        forecast=None,
        intraday=None,
        ensemble=None,
        event=SimpleNamespace(active_markets=[TICKER]),
        markets=[],
        event_title="two-level test event",
        market_available=True,
        probabilities={},
        consensus=None,
        risk_profile="live",
        paper_bankroll=1000.0,
        decisions=list(decisions),
    )

    def spy(name: str, *, call_real: bool = True):
        real = getattr(scan_module, name)

        def wrapper(*args, **kwargs):
            calls.append((name, time.monotonic(), kwargs))
            return real(*args, **kwargs) if call_real else None

        return wrapper

    def record_decisions(_target_date, recorded, **_kwargs):
        calls.append(("record_decisions", time.monotonic(), {"decisions": list(recorded)}))
        time.sleep(record_delay)
        return []

    replacements = {
        "build_scan_context": lambda *_a, **_k: context,
        "build_arbitrage_opportunities": lambda *_a, **_k: [],
        "allocate_portfolio": lambda *_a, **_k: plan,
        "_paper_entry_gate_for_target": lambda *_a, **_k: (True, None),
        "_cached_paper_entry_pause_reason": lambda *_a, **_k: None,
        "_print_portfolio_scan": lambda *_a, **_k: None,
        "_attach_pre_entry_ask_ladders": spy("_attach_pre_entry_ask_ladders"),
        "_restate_recorded_execution": spy("_restate_recorded_execution"),
        "_place_portfolio_orders": spy("_place_portfolio_orders"),
        "_capture_orderbook_depth_for_legs": spy(
            "_capture_orderbook_depth_for_legs", call_real=real_telemetry
        ),
    }
    args = argparse.Namespace(
        place_paper=True,
        paper_entry_mode="limit",
        max_arb_spend=None,
        min_profit=0.0,
        skip_context_snapshots=True,
    )
    with contextlib.ExitStack() as stack:
        for name, replacement in replacements.items():
            stack.enter_context(patch.object(scan_module, name, replacement))
        stack.enter_context(patch.object(store, "record_decisions", record_decisions))
        scan_module._portfolio_scan_one_target(
            args,
            date.fromisoformat(TARGET_DATE),
            Mock(),
            Mock(),
            strategy_config_for_profile("live"),
            store,
            Mock(),
            city=city,
            kalshi_client=client,
            pause_reasons={},
        )


def _paper_orders(store: PaperStore) -> list[sqlite3.Row]:
    with store.connect() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM paper_orders ORDER BY id").fetchall()


def test_portfolio_scan_records_and_places_the_ladder_it_fetched():
    """Executes the scan chain instead of reading its source (review 2026-09-13).

    The ladder on ``plan.legs`` must reach BOTH payloads restatement joins --
    the journalled decision and the placed order's quote snapshot and signal
    -- and a slow recording write must not eat the telemetry budget: only the
    pre-entry fetch time is charged to it.
    """

    client = Mock()
    client.get_orderbook.return_value = LADDER_FIXTURE
    calls: list = []
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        _run_portfolio_scan(
            store, client, [_decision(ask_levels=None)], calls=calls, record_delay=1.2
        )
        rows = _paper_orders(store)

    assert [name for name, _, _ in calls] == [
        "_attach_pre_entry_ask_ladders",
        "_restate_recorded_execution",
        "record_decisions",
        "_place_portfolio_orders",
        "_capture_orderbook_depth_for_legs",
    ]
    client.get_orderbook.assert_called_once_with(TICKER, depth=3)
    by_name = {name: kwargs for name, _, kwargs in calls}
    (journal,) = [
        d for d in by_name["record_decisions"]["decisions"] if d.ticker == TICKER
    ]
    assert journal.ask_levels == NO_LADDER
    assert journal.taker_levels_used == 2
    assert journal.limit_price == 0.83
    assert journal.recommended_contracts == 30.0
    # The scan hands the restatement placement's own inputs, so the account
    # fit the journal applies is the one placement applies.
    restatement = by_name["_restate_recorded_execution"]
    assert restatement["target_date"] == TARGET_DATE
    assert restatement["bankroll"] > 0
    assert len(rows) == 1
    assert (rows[0]["contracts"], rows[0]["entry_price"]) == (30.0, 0.83)
    walked = [[0.82, 4.0], [0.83, 31.0]]
    assert json.loads(rows[0]["quote_snapshot_json"])["ask_levels"] == walked
    assert json.loads(rows[0]["diagnostics_json"])["signal"]["ask_levels"] == walked
    telemetry = by_name["_capture_orderbook_depth_for_legs"]
    assert telemetry["skip_tickers"] == {TICKER}
    # Charged for the millisecond prefetch, not the 1.2s recording write.
    assert telemetry["budget_seconds"] > scan_module._ORDERBOOK_CAPTURE_BUDGET_SECONDS - 0.5


def test_slow_ladder_fetch_never_delays_placement_beyond_its_deadline():
    """A hung orderbook call costs at most one deadline; then placement runs."""

    deadline = scan_module._PRE_ENTRY_LADDER_DEADLINE_SECONDS
    assert 0.0 < deadline <= 3.0
    release = threading.Event()

    def _hang(*_args, **_kwargs):
        release.wait(30.0)
        return LADDER_FIXTURE

    client = Mock()
    client.get_orderbook.side_effect = _hang
    other = _decision(ask_levels=None, ticker="KXHIGHNY-26SEP14-B78.5", label="78° to 79°")
    calls: list = []
    try:
        with TemporaryDirectory() as tmp:
            store = PaperStore(Path(tmp) / "paper.db")
            started = time.monotonic()
            _run_portfolio_scan(
                store,
                client,
                [_decision(ask_levels=None), other],
                calls=calls,
                real_telemetry=False,
            )
            rows = _paper_orders(store)
    finally:
        release.set()

    placed_at = next(at for name, at, _ in calls if name == "_place_portfolio_orders")
    assert placed_at - started < deadline + 1.0
    # The first timeout ends pre-entry fetching for the target: one deadline, not two.
    assert client.get_orderbook.call_count == 1
    assert rows
    for row in rows:
        assert (row["status"], row["contracts"], row["entry_price"]) == (
            "PAPER_FILLED",
            4.0,
            0.82,
        )
        quote = json.loads(row["quote_snapshot_json"])
        assert quote["taker_levels_used"] == 1
        assert "ask_levels" not in quote
    telemetry = next(
        kwargs for name, _, kwargs in calls if name == "_capture_orderbook_depth_for_legs"
    )
    assert telemetry["skip_tickers"] == {TICKER}


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


# ---------------------------------------------------------------------------
# pre-entry fetch latency (review 2026-09-13)
# ---------------------------------------------------------------------------


def test_capture_within_a_deadline_abandons_a_hung_fetch_and_never_raises():
    release = threading.Event()

    def _hang(*_args, **_kwargs):
        release.wait(30.0)
        return LADDER_FIXTURE

    hung = Mock()
    hung.get_orderbook.side_effect = _hang
    try:
        started = time.monotonic()
        result = capture_orderbook_depth_within(
            hung, TICKER, levels=3, deadline_seconds=0.2
        )
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert result == (None, True)
    assert elapsed < 1.0

    prompt = Mock()
    prompt.get_orderbook.return_value = LADDER_FIXTURE
    depth, timed_out = capture_orderbook_depth_within(
        prompt, TICKER, levels=3, deadline_seconds=2.0
    )
    assert timed_out is False
    assert depth == parse_orderbook_response(LADDER_FIXTURE)
    prompt.get_orderbook.assert_called_once_with(TICKER, depth=3)

    failing = Mock()
    failing.get_orderbook.side_effect = OSError("network down")
    assert capture_orderbook_depth_within(failing, TICKER, deadline_seconds=2.0) == (
        None,
        False,
    )
    idle = Mock()
    for no_time in (0.0, -1.0, float("nan")):
        assert capture_orderbook_depth_within(
            idle, TICKER, deadline_seconds=no_time
        ) == (None, True)
    idle.get_orderbook.assert_not_called()


@pytest.mark.parametrize("failure", ["rate_limited", "socket_timeout"])
def test_pre_entry_fetch_is_one_short_attempt_even_on_the_shared_client(failure):
    """The scan client's defaults (20s x 3 with backoff, Retry-After) never apply."""

    socket_timeouts: list = []

    def _urlopen(request, timeout=None):
        socket_timeouts.append(timeout)
        if failure == "rate_limited":
            raise HTTPError(
                request.full_url, 429, "Too Many Requests", {"Retry-After": "30"}, None
            )
        raise TimeoutError("read timed out")

    shared = KalshiPublicClient("https://kalshi.invalid/trade-api/v2")
    plan = _plan([_leg(_decision(ask_levels=None))])
    with patch("sfo_kalshi_quant.kalshi.urlopen", side_effect=_urlopen):
        started = time.monotonic()
        (attached, attempted), store = _attach(plan, client=shared)
        elapsed = time.monotonic() - started

    # One attempt, with the short socket timeout.
    assert socket_timeouts == [scan_module._PRE_ENTRY_LADDER_DEADLINE_SECONDS]
    assert elapsed < 1.0  # no backoff sleep, no 30s Retry-After wait
    assert attached.legs[0] is plan.legs[0]
    assert attempted == {TICKER}
    store.record_orderbook_depth.assert_not_called()
    # The scan's own client keeps its defaults for every other call.
    assert (shared.timeout, shared.retries, shared.backoff) == (20, 3, 0.5)


def test_pre_entry_ladder_client_ignores_a_stale_scan_client_binding(monkeypatch) -> None:
    # cli._sync_scan_bindings copies cli.KalshiPublicClient into _cli.scan, so
    # an earlier test that patched the cli name can leave a Mock bound there
    # (test_research_frequency does exactly that). The type check must still
    # recognise a real client and pass a test double through untouched.
    monkeypatch.setattr(scan_module, "KalshiPublicClient", Mock())
    real = KalshiPublicClient("https://kalshi.invalid/trade-api/v2")

    single = scan_module._pre_entry_ladder_client(real)

    assert isinstance(single, KalshiPublicClient)
    assert single is not real
    assert single.retries == 1
    assert single.timeout == scan_module._PRE_ENTRY_LADDER_DEADLINE_SECONDS
    double = object()
    assert scan_module._pre_entry_ladder_client(double) is double
