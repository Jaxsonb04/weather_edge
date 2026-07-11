"""Direct contract tests for the CLI's extracted domain modules."""

from types import SimpleNamespace
from collections.abc import Callable
from typing import get_type_hints


def test_format_module_owns_pnl_formatting() -> None:
    from sfo_kalshi_quant._cli.format import _format_pnl

    assert _format_pnl(None) == "open"
    assert _format_pnl(12.345) == "$12.35"
    assert _format_pnl(-0.5) == "$-0.50"
    assert _format_pnl.__module__ == "sfo_kalshi_quant._cli.format"


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
