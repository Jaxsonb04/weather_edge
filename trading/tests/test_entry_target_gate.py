from datetime import UTC, date, datetime

from sfo_kalshi_quant.cli import _paper_entry_gate_for_target
from sfo_kalshi_quant.cities import get_city
from sfo_kalshi_quant.config import SFO_TZ
from sfo_kalshi_quant.models import ForecastSnapshot, IntradaySnapshot


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
