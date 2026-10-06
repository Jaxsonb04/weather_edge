"""Exit evidence must retain quote age and conserve each account's depth."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from sfo_kalshi_quant.cli import build_parser
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import MarketBin, TradeDecision
from sfo_kalshi_quant.monitor import _monitor_market_lookup, run_paper_monitor


TARGET = "2026-10-05"
TICKER = "KXHIGHTSFO-26OCT05-B68.5"


def _decision(ticker=TICKER, *, contracts=8.0):
    return TradeDecision(
        ticker=ticker, label="68 to 69", action="BUY_YES", approved=True,
        probability=0.8, probability_lcb=0.7, yes_bid=0.39, yes_ask=0.4,
        spread=0.01, fee_per_contract=0.02, cost_per_contract=0.42,
        edge=0.38, edge_lcb=0.28, kelly_fraction=0.01,
        recommended_contracts=contracts, expected_profit=0.38 * contracts,
        reasons=[], side="YES", entry_bid_size=10.0, entry_ask_size=10.0,
        strike_type="between", floor_strike=68.0, cap_strike=69.0,
    )


def _market(ticker=TICKER):
    return MarketBin(
        ticker=ticker, event_ticker="KXHIGHTSFO-26OCT05",
        title="Highest temperature in San Francisco?", yes_sub_title="68 to 69",
        strike_type="between", floor_strike=68.0, cap_strike=69.0,
        yes_bid=0.9, yes_ask=0.91, no_bid=0.09, no_ask=0.1,
        yes_bid_size=10.0, yes_ask_size=10.0, status="active",
    )


def _args(path):
    return build_parser().parse_args(
        ["--db-path", str(path), "--no-color", "paper-monitor"]
    )


def _legacy_duplicate_positions(path, *, separate_accounts=False):
    store = PaperStore(path)
    first = store.record_paper_order(TARGET, _decision())
    second = store.record_paper_order(TARGET, _decision(TICKER + "-OTHER"))
    assert first is not None and second is not None
    # New entries cannot duplicate account/day/ticker/side. Exercise the
    # monitor's defensive handling of retained pre-guard/legacy lots.
    with store.connect() as conn:
        conn.execute("DROP INDEX ux_paper_orders_open_market_side_profile")
        conn.execute("UPDATE paper_orders SET market_ticker=? WHERE id=?", (TICKER, second))
        if separate_accounts:
            conn.execute("UPDATE paper_orders SET account_id='legacy-other' WHERE id=?", (second,))
    return store, first, second


@pytest.mark.parametrize("separate_accounts", [False, True])
def test_exit_depth_is_conserved_within_each_economic_account(tmp_path, separate_accounts):
    path = tmp_path / "paper.db"
    store, first, second = _legacy_duplicate_positions(path, separate_accounts=separate_accounts)

    class Client:
        def get_market(self, ticker):
            return _market(ticker)

    # Keep the legacy fixture outside new-entry/index migration. The monitor
    # neither admits nor merges these retained position lots.
    from unittest.mock import patch
    with patch("sfo_kalshi_quant.monitor.PaperStore", return_value=store):
        assert run_paper_monitor(_args(path), client_factory=Client) == 0

    with store.connect() as conn:
        executed = conn.execute(
            "SELECT SUM(contracts) FROM paper_orders WHERE status='PAPER_CLOSED'"
        ).fetchone()[0]
        open_quantity = conn.execute(
            "SELECT SUM(contracts) FROM paper_orders WHERE status='PAPER_FILLED'"
        ).fetchone()[0] or 0.0
        evidence = [json.loads(row[0])["exit_execution"] for row in conn.execute(
            "SELECT outcome_diagnostics_json FROM paper_orders WHERE status='PAPER_CLOSED'"
        )]
    assert executed == (16.0 if separate_accounts else 10.0)
    assert open_quantity == (0.0 if separate_accounts else 6.0)
    assert all(row["verification_status"] == "VERIFIED" for row in evidence)
    if not separate_accounts:
        assert sorted(row["displayed_depth"] for row in evidence) == [2.0, 10.0]


def test_quote_timestamp_is_request_start_with_fallback_own_clock():
    started = datetime.now(UTC)
    clock_values = iter([started, started + timedelta(seconds=30)])
    observed = {}

    class Client:
        def get_markets(self, tickers):
            return [_market(tickers[0])]

        def get_market(self, ticker):
            return _market(ticker)

    result = _monitor_market_lookup(
        Client(), ["A", "B"], observed_at_by_ticker=observed,
        clock=lambda: next(clock_values),
    )
    assert set(result) == {"A", "B"}
    assert observed == {
        "A": started.isoformat(),
        "B": (started + timedelta(seconds=30)).isoformat(),
    }


@pytest.mark.parametrize("elapsed", [30, 121])
def test_monitor_preserves_quote_time_and_refuses_stale_close(tmp_path, elapsed):
    path = tmp_path / "paper.db"
    store = PaperStore(path)
    order_id = store.record_paper_order(TARGET, _decision())
    observed = datetime.now(UTC)
    now = [observed]

    class Client:
        def get_market(self, ticker):
            now[0] += timedelta(seconds=elapsed)
            return _market(ticker)

    assert run_paper_monitor(
        _args(path), client_factory=Client, clock=lambda: now[0],
    ) == 0

    row = store._order(order_id)
    with store.connect() as conn:
        action = conn.execute(
            "SELECT action FROM paper_monitor_snapshots WHERE order_id=? ORDER BY id DESC LIMIT 1",
            (order_id,),
        ).fetchone()[0]
    if elapsed > 120:
        assert row["status"] == "PAPER_FILLED"
        assert action == "HOLD_STALE_QUOTE"
    else:
        assert row["status"] == "PAPER_CLOSED"
        evidence = json.loads(row["outcome_diagnostics_json"])["exit_execution"]
        assert evidence["observed_at"] == observed.isoformat()
        assert action == "CLOSE_TAKE_PROFIT"
