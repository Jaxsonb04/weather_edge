"""Tests for the closing-line-value / exit-drag measurement tool (Phase 0)."""

from pytest import approx

from sfo_kalshi_quant.clv import (
    OrderCLV,
    bin_resolves_yes,
    bucket_metrics,
    build_report,
    closing_line_value,
    counterfactual_pnl,
    side_won,
    temperature_cohort,
)


def test_bin_resolves_yes_matches_market_model_rules():
    # greater: YES iff high strictly above floor
    assert bin_resolves_yes("greater", 72.0, None, 73.0) is True
    assert bin_resolves_yes("greater", 72.0, None, 72.0) is False
    # less: YES iff high strictly below cap
    assert bin_resolves_yes("less", None, 70.0, 69.0) is True
    assert bin_resolves_yes("less", None, 70.0, 70.0) is False
    # range: inclusive on both ends
    assert bin_resolves_yes("range", 70.0, 72.0, 70.0) is True
    assert bin_resolves_yes("range", 70.0, 72.0, 72.0) is True
    assert bin_resolves_yes("range", 70.0, 72.0, 73.0) is False


def test_side_won_inverts_for_no_side():
    # A range bin 70-72 with settled high 71 resolves YES.
    assert side_won("YES", "range", 70.0, 72.0, 71.0) is True
    assert side_won("NO", "range", 70.0, 72.0, 71.0) is False
    # High of 75 -> bin resolves NO, so the NO side wins.
    assert side_won("NO", "range", 70.0, 72.0, 75.0) is True


def test_counterfactual_pnl_win_and_loss():
    # 10 contracts at cost 0.30: win returns 10*(1-0.3)=7.0, loss returns -3.0.
    assert counterfactual_pnl(10.0, 0.30, True) == 7.0
    assert counterfactual_pnl(10.0, 0.30, False) == -3.0


def test_closing_line_value_is_mark_minus_entry():
    assert closing_line_value(0.40, 0.55) == approx(0.15)
    assert closing_line_value(0.60, 0.45) == approx(-0.15)


def test_temperature_cohort_edges():
    assert temperature_cohort(69.0) == "cool_le_69f"
    assert temperature_cohort(70.0) == "warm_70_79f"
    assert temperature_cohort(79.0) == "warm_70_79f"
    assert temperature_cohort(80.0) == "hot_80f_plus"


def _order(**kw) -> OrderCLV:
    base = dict(
        order_id=1,
        target_date="2026-06-15",
        status="PAPER_CLOSED",
        side="NO",
        risk_profile="research",
        contracts=10.0,
        entry_cost=0.30,
        realized_pnl=-1.0,
        closing_mark=0.55,
        settlement_high_f=71.0,
        cohort="warm_70_79f",
        won=True,
        counterfactual_hold_pnl=7.0,
    )
    base.update(kw)
    return OrderCLV(**base)


def test_order_clv_derived_properties():
    order = _order()
    # CLV per contract = 0.55 - 0.30 = 0.25; total = 2.5
    assert order.clv_per_contract == approx(0.25)
    assert order.clv_total == approx(2.5)
    # Exit drag = realized (-1.0) - counterfactual (7.0) = -8.0: closing early
    # here forfeited a winner.
    assert order.exit_drag == approx(-8.0)


def test_exit_drag_only_for_closed_orders_with_known_high():
    held = _order(status="PAPER_SETTLED")
    assert held.exit_drag is None
    no_high = _order(settlement_high_f=None, counterfactual_hold_pnl=None, won=None)
    assert no_high.exit_drag is None
    assert no_high.clv_total == approx(2.5)  # CLV still available without a settlement high


def test_bucket_metrics_aggregates_and_tolerates_missing():
    records = [
        _order(order_id=1, realized_pnl=-1.0, counterfactual_hold_pnl=7.0, closing_mark=0.55),
        _order(order_id=2, realized_pnl=2.0, counterfactual_hold_pnl=-3.0, closing_mark=0.20),
        # No closing mark and no settlement -> contributes to orders count only.
        _order(
            order_id=3,
            realized_pnl=None,
            closing_mark=None,
            settlement_high_f=None,
            counterfactual_hold_pnl=None,
            won=None,
        ),
    ]
    block = bucket_metrics(records)
    assert block["orders"] == 3
    assert block["clv_covered"] == 2
    # CLV totals: order1 (0.25*10)=2.5, order2 (0.20-0.30)*10=-1.0 -> 1.5
    assert block["clv_total"] == approx(1.5)
    # Exit drag: order1 (-1-7)=-8, order2 (2-(-3))=5 -> -3
    assert block["exit_drag_total"] == approx(-3.0)
    assert block["exit_drag_covered"] == 2


def test_build_report_reports_settlement_coverage():
    records = [
        _order(order_id=1, target_date="2026-06-15", settlement_high_f=71.0),
        _order(
            order_id=2,
            target_date="2026-06-20",
            settlement_high_f=None,
            counterfactual_hold_pnl=None,
            won=None,
            cohort=None,
        ),
    ]
    report = build_report(records)
    cov = report["settlement_coverage"]
    assert cov["dates_total"] == 2
    assert cov["dates_with_authoritative_high"] == 1
    assert cov["uncovered_dates"] == ["2026-06-20"]
