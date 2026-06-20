from __future__ import annotations

from sfo_kalshi_quant.cli import build_parser, cmd_portfolio_scan


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
