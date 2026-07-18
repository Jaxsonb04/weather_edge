"""Direct contract tests for the CLI's extracted domain modules."""

from collections.abc import Callable
from datetime import date
import sqlite3
from types import SimpleNamespace
from typing import get_type_hints


def test_cli_reports_sqlite_lock_as_retryable_tempfail(monkeypatch, capsys) -> None:
    from sfo_kalshi_quant import cli

    def locked(_args) -> int:
        raise sqlite3.OperationalError("database is locked")

    parser = SimpleNamespace(parse_args=lambda _argv: SimpleNamespace(func=locked))
    monkeypatch.setattr(cli, "build_parser", lambda: parser)

    assert cli.main([]) == 75
    assert "temporary sqlite lock: database is locked" in capsys.readouterr().err


def test_format_module_owns_pnl_formatting() -> None:
    from sfo_kalshi_quant._cli.format import _format_pnl

    assert _format_pnl(None) == "open"
    assert _format_pnl(12.345) == "$12.35"
    assert _format_pnl(-0.5) == "$-0.50"
    assert _format_pnl.__module__ == "sfo_kalshi_quant._cli.format"


def test_format_module_renders_a_portfolio_without_scan_module_globals(capsys) -> None:
    from sfo_kalshi_quant._cli.format import _print_portfolio_scan
    from sfo_kalshi_quant.colors import Color

    forecast = SimpleNamespace(
        target_date=date(2026, 7, 11),
        predicted_high_f=70.0,
        source_spread_f=1.0,
        method="test",
        lead_hours=None,
        fresh_station_count=None,
        raw={},
        calls_used_today=None,
        max_calls_per_day=None,
        google_weight=None,
        nws_weight=None,
        open_meteo_weight=None,
        history_weight=None,
    )
    decision = SimpleNamespace(
        ticker="TEST",
        side="YES",
        label="70° or above",
        bid=0.4,
        ask=0.5,
        probability=0.6,
        probability_lcb=0.5,
        edge=0.1,
        edge_lcb=0.0,
        trade_quality_score=50.0,
        recommended_contracts=0.0,
        cost_per_contract=0.5,
        approved=False,
        reasons=["test rejection"],
    )
    plan = SimpleNamespace(
        approved=False,
        run_id="test-run",
        total_spend=0.0,
        expected_profit=0.0,
        worst_case_loss=0.0,
        risk_profile="live",
        limits=SimpleNamespace(max_daily_loss=10.0, yes_sleeve=2.0, explore_sleeve=0.0),
        reasons=[],
        legs=[],
    )

    _print_portfolio_scan(
        "Test event",
        forecast,
        plan,
        [decision],
        placed_ids=[],
        market_available=True,
        color=Color(enabled=False),
    )

    assert "TEST" not in capsys.readouterr().out


def test_monitor_module_owns_fill_model_and_exit_loop() -> None:
    from sfo_kalshi_quant.monitor import (
        _fill_resting_orders_against_live_book,
        run_paper_monitor,
    )

    assert _fill_resting_orders_against_live_book.__module__ == "sfo_kalshi_quant.monitor"
    assert run_paper_monitor.__module__ == "sfo_kalshi_quant.monitor"


def test_scan_module_owns_target_orchestration() -> None:
    from sfo_kalshi_quant._cli.scan import (
        _resolve_analysis_targets,
        cmd_analyze,
        cmd_arbitrage,
        cmd_portfolio_scan,
        cmd_tail_basket,
    )

    assert _resolve_analysis_targets.__module__ == "sfo_kalshi_quant._cli.scan"
    for command in (cmd_analyze, cmd_tail_basket, cmd_arbitrage, cmd_portfolio_scan):
        assert command.__module__ == "sfo_kalshi_quant._cli.scan"


def test_scan_command_defaults_honor_cli_city_and_bankroll_arguments() -> None:
    from sfo_kalshi_quant._cli.scan import default_scan_command_dependencies

    dependencies = default_scan_command_dependencies()
    args = SimpleNamespace(cities="nyc", risk_profile="research", bankroll=123.0)

    assert [city.slug for city in dependencies.cities_for_args(args)] == ["nyc"]
    assert dependencies.config_for_args(args).paper_bankroll == 123.0


def test_scan_intraday_helper_uses_the_city_settlement_day() -> None:
    from sfo_kalshi_quant._cli.scan import _intraday_for_target
    from sfo_kalshi_quant.cities import get_city
    from sfo_kalshi_quant.settlement_day import settlement_today

    city = get_city("sfo")
    args = SimpleNamespace(observed_high=None)

    class Adapter:
        @staticmethod
        def intraday_snapshot(_target):
            return None

    assert _intraday_for_target(
        args,
        settlement_today(city=city),
        Adapter(),
        city,
    ) is None


def test_scan_command_dependencies_have_parameterized_callable_types() -> None:
    from sfo_kalshi_quant._cli.scan import ScanCommandDependencies

    hints = get_type_hints(ScanCommandDependencies)
    assert hints
    assert all(hint is not Callable for hint in hints.values())


def test_paper_module_owns_settlement_commands() -> None:
    from sfo_kalshi_quant._cli.paper import cmd_paper_settle

    assert cmd_paper_settle.__module__ == "sfo_kalshi_quant._cli.paper"


def test_backtest_module_owns_rescore_command() -> None:
    from sfo_kalshi_quant._cli.backtest import cmd_backtest_rescore

    assert cmd_backtest_rescore.__module__ == "sfo_kalshi_quant._cli.backtest"


def test_parser_module_owns_argument_registration() -> None:
    from sfo_kalshi_quant._cli.parser import build_parser

    assert build_parser.__module__ == "sfo_kalshi_quant._cli.parser"


def test_data_module_owns_dataset_execution() -> None:
    from sfo_kalshi_quant._cli.data import cmd_collect, cmd_dataset_backfill

    assert cmd_collect.__module__ == "sfo_kalshi_quant._cli.data"
    assert cmd_dataset_backfill.__module__ == "sfo_kalshi_quant._cli.data"
