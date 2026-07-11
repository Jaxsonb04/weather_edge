from __future__ import annotations

import io
from contextlib import redirect_stdout
from datetime import date
from types import SimpleNamespace

import pytest

from sfo_kalshi_quant.cli import (
    _place_portfolio_orders,
    _print_portfolio_scan,
    build_parser,
    cmd_portfolio_scan,
)
from sfo_kalshi_quant.colors import Color
from sfo_kalshi_quant.models import ForecastSnapshot
from sfo_kalshi_quant.portfolio import PortfolioLimits, PortfolioPlan
from sfo_kalshi_quant.paper import ArbitrageContainmentError


def test_portfolio_scan_parser_is_paper_only_by_default() -> None:
    args = build_parser().parse_args(
        [
            "--bankroll",
            "1000",
            "--risk-profile",
            "live",
            "portfolio-scan",
        ]
    )

    assert args.func is cmd_portfolio_scan
    assert args.target_date == "rolling"
    assert args.side == "both"
    assert args.place_paper is False
    assert args.paper_entry_mode == "market"
    assert args.max_arb_spend == 12.0
    assert args.min_profit == 0.01


def test_paper_prune_help_marks_command_low_level_and_points_to_scheduled_service() -> None:
    help_text = build_parser().format_help()

    assert "Low-level/manual" in help_text
    assert "archive-gated service" in help_text


def test_portfolio_scan_parser_keeps_diagnostics_flags_available() -> None:
    args = build_parser().parse_args(
        [
            "--risk-profile",
            "research",
            "portfolio-scan",
            "--target-date",
            "both",
            "--side",
            "no",
            "--max-arb-spend",
            "20",
            "--min-profit",
            "0.05",
            "--paper-entry-mode",
            "limit",
            "--place-paper",
        ]
    )

    assert args.func is cmd_portfolio_scan
    assert args.target_date == "both"
    assert args.side == "no"
    assert args.max_arb_spend == 20.0
    assert args.min_profit == 0.05
    assert args.paper_entry_mode == "limit"
    assert args.place_paper is True


def test_portfolio_scan_prints_blocked_status_when_pause_prevents_placement() -> None:
    plan = PortfolioPlan(
        run_id="PF-test",
        risk_profile="research",
        approved=True,
        legs=[],
        arbitrage_opportunities=[],
        total_spend=12.34,
        worst_case_loss=12.34,
        expected_profit=1.23,
        reasons=[],
        limits=PortfolioLimits(
            risk_profile="research",
            bankroll=1000.0,
            max_daily_loss=250.0,
            yes_sleeve=50.0,
            explore_sleeve=12.5,
        ),
    )
    forecast = ForecastSnapshot(
        target_date=date(2026, 6, 20),
        predicted_high_f=68.0,
        method="fixture",
    )

    out = io.StringIO()
    with redirect_stdout(out):
        _print_portfolio_scan(
            "fixture event",
            forecast,
            plan,
            [],
            placed_ids=[],
            market_available=True,
            color=Color.from_no_color(True),
            entry_block_reason="research paused: daily loss cap reached",
        )

    text = out.getvalue()
    assert "research paused: daily loss cap reached" in text
    assert "portfolio=BLOCKED_BY_PAUSE" in text
    assert "portfolio=APPROVED" not in text


def test_portfolio_order_placement_contains_one_arbitrage_failure_and_continues() -> None:
    class _Trader:
        def __init__(self):
            self.arb_calls = 0
            self.directional_called = False

        def place_arbitrage(self, target_date, opportunity, *, bankroll):
            self.arb_calls += 1
            if self.arb_calls == 1:
                raise RuntimeError("simulated box race")
            return [22]

        def place_approved(self, target_date, decisions, *, bankroll):
            self.directional_called = True
            return [33]

    trader = _Trader()
    plan = SimpleNamespace(
        arbitrage_opportunities=[object(), object()],
        legs=[SimpleNamespace(sleeve="directional", decision=object())],
    )
    warnings: list[str] = []

    placed = _place_portfolio_orders(
        trader,
        "2026-06-03",
        plan,
        bankroll=1000.0,
        warn=warnings.append,
    )

    assert placed == [22, 33]
    assert trader.arb_calls == 2
    assert trader.directional_called
    assert warnings and "simulated box race" in warnings[0]


def test_portfolio_order_placement_stops_target_on_fatal_arbitrage_containment():
    class _Trader:
        directional_called = False

        def place_arbitrage(self, target_date, opportunity, *, bankroll):
            raise ArbitrageContainmentError("naked filled leg remains")

        def place_approved(self, target_date, decisions, *, bankroll):
            self.directional_called = True
            return [99]

    trader = _Trader()
    plan = SimpleNamespace(
        arbitrage_opportunities=[object()],
        legs=[SimpleNamespace(sleeve="directional", decision=object())],
    )

    with pytest.raises(ArbitrageContainmentError):
        _place_portfolio_orders(
            trader,
            "2026-06-03",
            plan,
            bankroll=1000.0,
        )

    assert trader.directional_called is False
