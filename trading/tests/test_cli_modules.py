"""Direct contract tests for the CLI's extracted domain modules."""


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
    from sfo_kalshi_quant._cli.scan import _resolve_analysis_targets

    assert _resolve_analysis_targets.__module__ == "sfo_kalshi_quant._cli.scan"


def test_paper_module_owns_settlement_commands() -> None:
    from sfo_kalshi_quant._cli.paper import cmd_paper_settle

    assert cmd_paper_settle.__module__ == "sfo_kalshi_quant._cli.paper"


def test_backtest_module_owns_rescore_command() -> None:
    from sfo_kalshi_quant._cli.backtest import cmd_backtest_rescore

    assert cmd_backtest_rescore.__module__ == "sfo_kalshi_quant._cli.backtest"


def test_parser_module_owns_argument_registration() -> None:
    from sfo_kalshi_quant._cli.parser import build_parser

    assert build_parser.__module__ == "sfo_kalshi_quant._cli.parser"
