from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sfo_kalshi_quant.cli import _paper_entry_gate_for_target
from sfo_kalshi_quant._cli import scan as scan_module
from sfo_kalshi_quant.cities import get_city
from sfo_kalshi_quant.config import SFO_TZ, strategy_config_for_profile
from sfo_kalshi_quant.models import ForecastSnapshot, IntradaySnapshot
from sfo_kalshi_quant.research_policy import MOTION_POLICY, TARGET_POLICY


def _forecast(target: date, raw=None, *, source_count: int = 4) -> ForecastSnapshot:
    return ForecastSnapshot(
        target_date=target,
        predicted_high_f=67.0,
        fetched_at="2026-06-08T18:00:00+00:00",
        source_count=source_count,
        raw=raw or {},
    )


def test_single_source_forecast_blocks_paper_entry():
    target = date(2026, 6, 9)
    now = datetime(2026, 6, 8, 12, 0, tzinfo=SFO_TZ)
    allowed, reason = _paper_entry_gate_for_target(
        target, _forecast(target, source_count=1), None, now=now
    )
    assert allowed is False
    assert reason is not None
    assert "single-source forecast" in reason


def test_same_day_entry_gate_blocks_after_peak_window():
    now = datetime(2026, 6, 8, 15, 1, tzinfo=SFO_TZ)
    allowed, reason = _paper_entry_gate_for_target(
        now.date(), _forecast(now.date()), None, now=now, risk_profile="research"
    )

    assert allowed is False
    assert reason is not None
    assert "local peak/high window has passed" in reason


def test_nyc_same_day_entry_gate_uses_fixed_est_cutoff_at_20z():
    city = get_city("nyc")
    now = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)  # 15:00 fixed EST
    target = datetime.fromtimestamp(now.timestamp(), city.fixed_standard_timezone()).date()

    allowed, reason = _paper_entry_gate_for_target(
        target,
        _forecast(target),
        None,
        city=city,
        now=now,
        risk_profile="research",
    )

    assert allowed is False
    assert reason is not None
    assert "local peak/high window has passed" in reason


def test_same_day_entry_gate_allows_later_targets_after_cutoff():
    now = datetime(2026, 6, 8, 15, 1, tzinfo=SFO_TZ)
    target = date(2026, 6, 9)
    allowed, reason = _paper_entry_gate_for_target(target, _forecast(target), None, now=now)

    assert allowed is True
    assert reason is None


def test_research_same_day_entry_gate_allows_observed_high_lock_before_peak_window():
    target = date(2026, 6, 8)
    now = datetime(2026, 6, 8, 2, 39, tzinfo=SFO_TZ)
    forecast = _forecast(target, {"observed_high_decision": {"mode": "lock"}})
    allowed, reason = _paper_entry_gate_for_target(
        target, forecast, None, now=now, risk_profile="research"
    )

    assert allowed is True
    assert reason is None


def test_live_profile_requires_at_least_next_day_target():
    target = date(2026, 6, 8)
    now = datetime(2026, 6, 8, 2, 39, tzinfo=SFO_TZ)
    allowed, reason = _paper_entry_gate_for_target(
        target, _forecast(target), None, now=now, risk_profile="live"
    )
    assert allowed is False
    assert reason == "live paper entry requires min_lead_days=1; same-day signals are research-only"


def test_same_day_entry_gate_blocks_complete_intraday_high():
    target = date(2026, 6, 8)
    now = datetime(2026, 6, 8, 12, 0, tzinfo=SFO_TZ)
    intraday = IntradaySnapshot(
        target_date=target,
        observed_high_f=67.0,
        latest_temp_f=66.0,
        latest_observed_at="2026-06-08T19:00:00+00:00",
        remaining_forecast_high_f=None,
        forecast_fetched_at="2026-06-08T19:00:00+00:00",
        is_complete=True,
    )
    allowed, reason = _paper_entry_gate_for_target(
        target, _forecast(target), intraday, now=now, risk_profile="research"
    )

    assert allowed is False
    assert reason is not None
    assert "official daily high is complete" in reason


def test_research_scan_passes_one_shared_opportunity_set_to_both_books() -> None:
    target = date(2026, 7, 19)
    decisions = [object(), object(), object()]
    target_decisions = [object(), object(), object()]
    motion_decisions = [object(), object(), object()]
    context = SimpleNamespace(
        decisions=decisions,
        series_ticker="KXHIGHTSFO",
        intraday=None,
        forecast=object(),
        event=object(),
        consensus=object(),
    )
    store = Mock()
    store.research_objective_day.return_value = date(2026, 7, 18)
    store.research_account_state.side_effect = (
        {"available_cash": 900.0},
        {"available_cash": 800.0},
    )
    store.research_realized_pnl_for_day.side_effect = (12.0, -3.0)
    plans = SimpleNamespace(target=object(), motion=object())
    execution = object()
    trader = Mock()
    trader.execute_research_plans.return_value = execution

    with (
        patch.object(
            scan_module,
            "prepare_research_sleeve_decisions",
            return_value=(target_decisions, motion_decisions),
        ) as prepare,
        patch.object(scan_module, "ResearchOpportunity", side_effect=lambda d, t, l: (d, t, l)),
        patch.object(scan_module, "allocate_research_plans", return_value=plans) as allocate,
        patch.object(scan_module, "PaperTrader", return_value=trader) as trader_type,
    ):
        actual_plans, actual_execution, recorded = scan_module._execute_research_scan_context(
            context,
            target=target,
            store=store,
            config=strategy_config_for_profile("research"),
            entry_allowed=True,
            entry_block_reason=None,
            place_paper=False,
            place_research_target=True,
            place_research_motion=False,
            forecast_snapshot_id=10,
            market_snapshot_id=20,
            scan_run_id="one-shared-scan",
        )

    assert actual_plans is plans
    assert actual_execution is execution
    assert recorded == decisions
    prepare.assert_called_once()
    assert prepare.call_args.args[0] == decisions
    allocate.assert_called_once_with(
        [(decision, "2026-07-19", 1) for decision in target_decisions],
        motion_opportunities=[
            (decision, "2026-07-19", 1) for decision in motion_decisions
        ],
        target_available_cash=900.0,
        motion_available_cash=800.0,
        realized_today=12.0,
        motion_realized_today=-3.0,
        run_id="one-shared-scan",
    )
    assert trader_type.call_count == 1
    call = trader.execute_research_plans.call_args
    assert call.args[0] == "2026-07-19"
    assert call.args[1] is plans
    assert call.kwargs["source_decisions"] == target_decisions
    assert call.kwargs["motion_source_decisions"] == motion_decisions
    assert call.kwargs["scan_run_id"] == "one-shared-scan"
    assert call.kwargs["admit_orders"] is False
    assert call.kwargs["admit_target_orders"] is True
    assert call.kwargs["admit_motion_orders"] is False
    assert store.research_account_state.call_args_list[0].kwargs == {
        "account_id": TARGET_POLICY.account_id
    }
    assert store.research_account_state.call_args_list[1].kwargs == {
        "account_id": MOTION_POLICY.account_id
    }
