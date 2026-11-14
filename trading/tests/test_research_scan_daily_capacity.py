"""Scheduled planning bounds v8 entries by the realized daily-loss room."""

from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from sfo_kalshi_quant._cli import scan as scan_module
from sfo_kalshi_quant.cities import get_city
from sfo_kalshi_quant.config import strategy_config_for_profile
from sfo_kalshi_quant.research_entry_risk import TARGET_OPEN_RISK_CHARGE_FRACTION
from test_target_execution_capacity import _candidate

# 13:00 PDT on D-1 for the 2026-09-12 SFO target: civil lead 1, not the
# 10:00 New York listing hour, so the v8 market-signal copy is tradeable.
QUOTE_AT = datetime(2026, 9, 11, 20, 0, tzinfo=UTC)


def _scan(state, *, realized=0.0):
    context = SimpleNamespace(
        # A NO leg the M1 recalibration prices well above its 0.76 ask.
        decisions=[replace(_candidate(), market_probability=0.85)],
        city=get_city("sfo"),
        series_ticker="KXHIGHTSFO",
        intraday=None,
        forecast=object(),
        event=object(),
        consensus=object(),
        quote_observed_at=QUOTE_AT,
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


@pytest.mark.parametrize("open_cost,reservations", [(450.0, 0.0), (0.0, 450.0), (180.0, 270.0)])
def test_scan_open_and_pending_cost_do_not_shrink_the_flat_entry(open_cost, reservations):
    plans, store = _scan({
        "available_cash": 900.0,
        "open_cost_basis": open_cost,
        "reservations": reservations,
    })
    # v8 holds to settlement: open cost is not charged against the $500 room,
    # so the full flat $90 leg is planned.
    assert len(plans.target.legs) == 1
    assert 89.0 < plans.target.total_spend <= 90.0
    assert store.research_account_state.call_count == 1


def test_planning_cash_is_the_realized_room_capped_by_cash():
    assert TARGET_OPEN_RISK_CHARGE_FRACTION == 0.0
    filled = scan_module._target_planning_cash(
        {"available_cash": 900.0, "open_cost_basis": 200.0, "reservations": 0.0},
        0.0,
    )
    resting = scan_module._target_planning_cash(
        {"available_cash": 900.0, "open_cost_basis": 0.0, "reservations": 200.0},
        0.0,
    )
    assert filled == resting == pytest.approx(500.0)
    # Profits never expand the room, and it never exceeds spendable cash.
    assert scan_module._target_planning_cash(
        {"available_cash": 900.0, "open_cost_basis": 0.0, "reservations": 0.0},
        40.0,
    ) == pytest.approx(500.0)
    assert scan_module._target_planning_cash(
        {"available_cash": 12.5, "open_cost_basis": 0.0, "reservations": 200.0},
        0.0,
    ) == pytest.approx(12.5)


def test_scan_subtracts_realized_losses_from_the_daily_room():
    plans, _ = _scan({
        "available_cash": 900.0,
        "open_cost_basis": 180.0,
        "reservations": 270.0,
    }, realized=-490.0)
    # $500 - $490 realized = $10.
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
