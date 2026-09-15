"""Scheduled planning must reserve existing filled and pending daily risk."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from sfo_kalshi_quant._cli import scan as scan_module
from sfo_kalshi_quant.cities import get_city
from sfo_kalshi_quant.config import strategy_config_for_profile
from sfo_kalshi_quant.research_entry_risk import TARGET_OPEN_RISK_CHARGE_FRACTION
from test_target_execution_capacity import _candidate


def _scan(state, *, realized=0.0):
    context = SimpleNamespace(
        decisions=[_candidate()],
        city=get_city("sfo"),
        series_ticker="KXHIGHTSFO",
        intraday=None,
        forecast=object(),
        event=object(),
        consensus=object(),
    )
    store = Mock()
    store.research_objective_day.return_value = date(2026, 9, 11)
    # SFO's fixed-standard station day agrees with the Pacific civil day
    # here, so the target stays a day-ahead candidate on both clocks.
    store.research_station_day.return_value = date(2026, 9, 11)
    store.research_account_state.return_value = state
    store.research_realized_pnl_for_day.return_value = realized
    trader = Mock()
    with patch.object(scan_module, "PaperTrader", return_value=trader):
        plans, _, _ = scan_module._execute_research_scan_context(
            context,
            target=date(2026, 9, 12),
            store=store,
            config=strategy_config_for_profile("research"),
            entry_allowed=True,
            entry_block_reason=None,
            place_paper=True,
            forecast_snapshot_id=1,
            market_snapshot_id=2,
        )
    return plans, store


@pytest.mark.parametrize("open_cost,reservations", [(200.0, 0.0), (0.0, 200.0), (80.0, 120.0)])
def test_scan_shrinks_planned_entry_to_remaining_daily_risk(open_cost, reservations):
    plans, store = _scan({
        "available_cash": 900.0,
        "open_cost_basis": open_cost,
        "reservations": reservations,
    })
    # $200 of open/pending cost is charged at the 0.60 stop-scaled fraction,
    # so a $90 desired entry must fit the remaining $150 - $120 = $30. A
    # partially filled order's $80 filled + $120 pending risk counts once
    # each, exactly like the existing account-state query, and a resting
    # reservation is charged at the same rate as filled cost.
    assert len(plans.target.legs) == 1
    assert 29.0 < plans.target.total_spend <= 30.0
    assert store.research_account_state.call_count == 1


def test_planning_cash_charges_reservations_like_filled_cost_at_stop_fraction():
    assert TARGET_OPEN_RISK_CHARGE_FRACTION == 0.60
    filled = scan_module._target_planning_cash(
        {"available_cash": 900.0, "open_cost_basis": 100.0, "reservations": 0.0},
        0.0,
    )
    resting = scan_module._target_planning_cash(
        {"available_cash": 900.0, "open_cost_basis": 0.0, "reservations": 100.0},
        0.0,
    )
    assert filled == pytest.approx(90.0)
    assert resting == filled
    # The room never exceeds spendable cash.
    assert scan_module._target_planning_cash(
        {"available_cash": 12.5, "open_cost_basis": 0.0, "reservations": 100.0},
        0.0,
    ) == pytest.approx(12.5)


def test_scan_subtracts_realized_losses_from_existing_exposure_room():
    plans, _ = _scan({
        "available_cash": 900.0,
        "open_cost_basis": 80.0,
        "reservations": 120.0,
    }, realized=-20.0)
    # $150 - $20 realized - 0.6 * $200 = $10.
    assert 9.0 < plans.target.total_spend <= 10.0


@pytest.mark.parametrize(
    "state",
    [
        None,
        {"available_cash": 900.0},
        {"available_cash": 900.0, "open_cost_basis": -1.0, "reservations": 0.0},
        {"available_cash": 900.0, "open_cost_basis": 1.0, "reservations": float("nan")},
        {"available_cash": 900.0, "open_cost_basis": True, "reservations": 0.0},
        {"available_cash": float("inf"), "open_cost_basis": 0.0, "reservations": 0.0},
    ],
)
def test_scan_fails_closed_without_complete_finite_account_risk(state):
    plans, _ = _scan(state)
    assert plans.target.legs == []


def test_realized_profits_do_not_expand_daily_room():
    plans, _ = _scan({
        "available_cash": 940.0,
        "open_cost_basis": 200.0,
        "reservations": 0.0,
    }, realized=40.0)
    assert 29.0 < plans.target.total_spend <= 30.0
