"""Live and research v8 trade the M1 market recalibration (2026-11-12)."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from sfo_kalshi_quant._cli import scan as scan_module
from sfo_kalshi_quant.account import LIVE_STABILITY_ACCOUNT_ID, SHARED_ACCOUNT_ID
from sfo_kalshi_quant.cities import get_city
from sfo_kalshi_quant.config import strategy_config_for_profile
from sfo_kalshi_quant.execution import target_research_quote
from sfo_kalshi_quant.fees import quadratic_fee_average_per_contract
from sfo_kalshi_quant.models import BucketProbability, ForecastSnapshot
from sfo_kalshi_quant.monitor import _hold_to_settlement_reason
from sfo_kalshi_quant.research_m1 import (
    M1_ACCOUNT_ID,
    M1LegInputs,
    evaluate_leg,
    recalibrated_p_yes,
)
from sfo_kalshi_quant.research_policy import (
    MOTION_POLICY,
    TARGET_POLICY,
    TARGET_POLICY_V7,
)
from sfo_kalshi_quant.risk import TradeEvaluator
from sfo_kalshi_quant.standard_bins import standard_sfo_bins
from test_target_execution_capacity import _candidate

SFO = get_city("sfo")
TARGET = date(2026, 11, 13)
# 13:00 PST on D-1: civil lead 1, bucket L1b, not the New York listing hour.
QUOTE_AT = datetime(2026, 11, 12, 21, 0, tzinfo=UTC)


def _ladder(*, yes_market: float, no_ask: float):
    market = next(row for row in standard_sfo_bins() if row.yes_sub_title == "66° to 67°")
    market = replace(
        market,
        status="active",
        yes_bid=round(1.0 - no_ask, 2),
        yes_ask=round(1.0 - no_ask + 0.02, 2),
        no_bid=round(no_ask - 0.02, 2),
        no_ask=no_ask,
        yes_bid_size=200.0,
        yes_ask_size=200.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.40,
        lower_confidence=0.30,
        empirical_probability=0.40,
        normal_probability=0.40,
        effective_n=250,
        model_probability=0.30,
        market_probability=yes_market,
    )
    return market, probability


# --- Live -------------------------------------------------------------------


def test_live_day_ahead_probability_is_the_m1_recalibration_and_keeps_audit_reads():
    _, prob = _ladder(yes_market=0.25, no_ask=0.70)
    unpriced = replace(prob, ticker="UNPRICED", market_probability=None)
    rewritten = scan_module._m1_live_probabilities(
        {prob.ticker: prob, "UNPRICED": unpriced}, TARGET, SFO, QUOTE_AT
    )
    p = recalibrated_p_yes(0.25, "L1b")
    live = rewritten[prob.ticker]
    assert live.probability == p
    assert live.lower_confidence == pytest.approx(p - 0.02)
    assert live.upper_confidence == pytest.approx(p + 0.02)
    assert (live.model_probability, live.market_probability) == (0.30, 0.25)
    assert rewritten["UNPRICED"] is unpriced
    # Other leads keep the blend; the entry gate blocks them.
    for target in (date(2026, 11, 12), date(2026, 11, 14)):
        assert scan_module._m1_live_probabilities({prob.ticker: prob}, target, SFO, QUOTE_AT) == {
            prob.ticker: prob
        }


@pytest.mark.parametrize("no_ask", [0.70, 0.72])
def test_live_approval_matches_the_m1_entry_rule(no_ask):
    market, prob = _ladder(yes_market=0.25, no_ask=no_ask)
    live_prob = scan_module._m1_live_probabilities(
        {prob.ticker: prob}, TARGET, SFO, QUOTE_AT
    )[prob.ticker]
    decision = TradeEvaluator(strategy_config_for_profile("live")).evaluate_market(
        market, live_prob, bankroll=2000.0, side="NO"
    )
    m1 = evaluate_leg(
        M1LegInputs(
            created_at=QUOTE_AT,
            target_date=TARGET,
            market_ticker=market.ticker,
            side="NO",
            civil_tz=SFO.civil_tz_name,
            market_probability=1.0 - prob.market_probability,
            model_probability=1.0 - prob.model_probability,
            ask=market.no_ask,
            bid=market.no_bid,
            ask_size=200.0,
            bid_size=200.0,
            market_status="active",
            market_close_time=None,
            forecast_age_hours=1.0,
            source_spread_f=1.0,
        )
    )
    # 0.70: edge 0.032 >= 0.02, both enter; 0.72: edge 0.012, neither does.
    assert decision.approved is m1.signal is (no_ask == 0.70), decision.reasons


def test_live_entry_gate_blocks_lead_two_and_keeps_the_same_day_block():
    forecast = ForecastSnapshot(
        target_date=TARGET, predicted_high_f=60.0, fetched_at=QUOTE_AT.isoformat(), source_count=4
    )
    gate = scan_module._paper_entry_gate_for_target
    assert gate(TARGET, forecast, None, city=SFO, now=QUOTE_AT, risk_profile="live") == (True, None)
    assert gate(date(2026, 11, 14), forecast, None, city=SFO, now=QUOTE_AT, risk_profile="live") == (
        False,
        "live paper entry requires civil lead 1; the M1 recalibration is fitted "
        "for day-ahead (D-1) only",
    )
    assert gate(date(2026, 11, 12), forecast, None, city=SFO, now=QUOTE_AT, risk_profile="live")[1] == (
        "live paper entry requires min_lead_days=1; same-day signals are research-only"
    )
    # 10:30 EST on D-1: M1's listing-hour skip applies to Live too; 11:00 is open.
    listing = datetime(2026, 11, 12, 15, 30, tzinfo=UTC)
    assert gate(TARGET, forecast, None, city=SFO, now=listing, risk_profile="live") == (
        False,
        "live paper entry skips the listing hour (D-1 10:00-10:59 America/New_York), "
        "where M1's test showed taker losses",
    )
    assert gate(
        TARGET, forecast, None, city=SFO, now=listing + timedelta(minutes=30), risk_profile="live"
    ) == (True, None)
    # Research keeps its own lead rules.
    assert gate(date(2026, 11, 14), forecast, None, city=SFO, now=QUOTE_AT, risk_profile="research") == (
        True,
        None,
    )


def test_live_drops_the_weather_model_shaping_gates():
    live = strategy_config_for_profile("live")
    assert live.favorite_band_enabled is False
    assert live.comfort_edge_enabled is False
    assert live.min_edge_lcb == 0.0


# --- Research v8 ----------------------------------------------------------


def _v8_copy(decision, target=TARGET, at=QUOTE_AT):
    return scan_module._research_market_signal_decisions(
        [decision], target=target, city=SFO, observed_at=at
    )[0]


def test_v8_copies_price_on_m1_and_leave_the_original_decisions_alone():
    original = replace(_candidate(), market_probability=0.85)
    before = replace(original)
    copy = _v8_copy(original)
    p_no = 1.0 - recalibrated_p_yes(0.15, "L1b")
    assert copy.probability == pytest.approx(p_no)
    assert copy.probability_lcb == pytest.approx(p_no - 0.01)
    assert copy.model_probability == original.model_probability
    assert original == before


@pytest.mark.parametrize(
    "target,at,reason",
    [
        (date(2026, 11, 14), QUOTE_AT, "research v8 trades day-ahead only (civil lead 1)"),
        (date(2026, 11, 12), QUOTE_AT, "research v8 trades day-ahead only (civil lead 1)"),
        # 10:30 EST on D-1: the hour after Kalshi lists the next ladder.
        (
            TARGET,
            datetime(2026, 11, 12, 15, 30, tzinfo=UTC),
            "research v8 skips the listing hour (D-1 10:00-10:59 America/New_York)",
        ),
    ],
)
def test_v8_trades_day_ahead_only_and_skips_the_listing_hour(target, at, reason):
    copy = _v8_copy(replace(_candidate(), market_probability=0.85), target=target, at=at)
    assert copy.approved is False
    assert copy.entry_block_reason == reason


def test_v8_needs_a_market_probability():
    copy = _v8_copy(_candidate())
    assert copy.approved is False
    assert copy.entry_block_reason == "research v8 needs the scan's market probability"


def test_v8_enters_taker_only_at_a_one_cent_after_fee_edge():
    config = strategy_config_for_profile("research")
    copy = _v8_copy(replace(_candidate(), market_probability=0.85, recommended_contracts=10.0))
    checked = 0
    for cents in range(70, 95):
        ask = cents / 100.0
        leg = replace(copy, entry_ask=ask, entry_bid=ask - 0.01, entry_ask_size=10.0)
        quote = target_research_quote(leg, config)
        fee = quadratic_fee_average_per_contract(
            ask, 10.0, maker=False, taker_rate=config.taker_fee_rate,
            maker_rate=config.maker_fee_rate, series_ticker=leg.ticker,
        )
        enters = copy.probability - (ask + fee) >= 0.01 - 1e-12
        assert (quote is not None) is enters, ask
        if quote is not None:
            assert quote.would_cross and quote.price == ask
        checked += enters
    assert 0 < checked < 25  # the threshold falls inside the sweep


def test_research_scan_feeds_m1_the_original_decisions_and_v8_its_copies():
    original = replace(_candidate(), market_probability=0.85)
    context = SimpleNamespace(
        decisions=[original],
        markets=[],
        probabilities={},
        city=SFO,
        series_ticker=SFO.series_ticker,
        intraday=None,
        forecast=SimpleNamespace(source_count=4),
        event=SimpleNamespace(active_markets=[object()], title="t"),
        consensus=None,
        quote_observed_at=QUOTE_AT,
        event_title="t",
        market_available=True,
        ensemble=None,
    )
    store = Mock()
    store.research_objective_day.return_value = date(2026, 11, 12)
    store.research_station_day.return_value = date(2026, 11, 12)
    store.research_account_state.return_value = {
        "available_cash": 900.0, "open_cost_basis": 0.0, "reservations": 0.0,
    }
    store.research_realized_pnl_for_day.return_value = 0.0
    trader = Mock()
    trader.execute_research_plans.return_value = SimpleNamespace(
        target_order_ids=(), motion_order_ids=()
    )
    m1_inputs = {}

    def fake_m1(_store, **kwargs):
        m1_inputs.update(kwargs)
        return SimpleNamespace(skipped_reason="test")

    args = SimpleNamespace(
        place_paper=False, place_research_target=False, place_research_m1=False,
        risk_profile="research", skip_context_snapshots=True,
    )
    with (
        patch.object(scan_module, "PaperTrader", return_value=trader),
        patch.object(scan_module, "run_m1_scan", side_effect=fake_m1),
        patch.object(scan_module, "_print_portfolio_scan"),
        patch.object(
            scan_module,
            "prepare_research_target_decisions",
            wraps=scan_module.prepare_research_target_decisions,
        ) as prepare,
    ):
        scan_module._research_portfolio_scan_from_context(
            args, TARGET, context, strategy_config_for_profile("research"), store, Mock()
        )

    # M1's frozen trial sees the scan's own decisions, untouched.
    assert m1_inputs["decisions"] == [original]
    assert m1_inputs["decisions"][0] is original
    # V8 executes on its recalibrated copies.
    (v8_decision,) = prepare.call_args.args[0]
    assert v8_decision is not original
    assert v8_decision.probability == pytest.approx(1.0 - recalibrated_p_yes(0.15, "L1b"))
    assert v8_decision.model_probability == original.model_probability


# --- Hold to settlement -----------------------------------------------------


@pytest.mark.parametrize(
    "account_id,held",
    [
        (M1_ACCOUNT_ID, True),
        (LIVE_STABILITY_ACCOUNT_ID, True),
        (TARGET_POLICY.account_id, True),
        (TARGET_POLICY_V7.account_id, False),
        (MOTION_POLICY.account_id, False),
        (SHARED_ACCOUNT_ID, False),
        (None, False),
    ],
)
def test_hold_to_settlement_covers_the_market_signal_books_only(account_id, held):
    reason = _hold_to_settlement_reason({"account_id": account_id})
    assert (reason is not None) is held
    if account_id == M1_ACCOUNT_ID:
        assert reason == "M1 market-recalibration trial: held to settlement by policy"
