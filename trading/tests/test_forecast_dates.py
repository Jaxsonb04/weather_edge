from datetime import date, datetime

from sfo_kalshi_quant.cli import _completed_open_target_dates, _rolling_live_event_targets
from sfo_kalshi_quant.config import SFO_TZ
from sfo_kalshi_quant.forecast import parse_target_dates
from sfo_kalshi_quant.models import EventSnapshot
from sfo_kalshi_quant.settlement_day import settlement_today


def test_parse_target_dates_accepts_explicit_comma_list():
    targets = parse_target_dates("2026-06-03,2026-06-04")
    assert targets == [date(2026, 6, 3), date(2026, 6, 4)]


def test_parse_target_dates_both_returns_two_dates():
    targets = parse_target_dates("both")
    assert len(targets) == 2
    assert (targets[1] - targets[0]).days == 1


def test_parse_target_dates_rolling_returns_three_dates():
    targets = parse_target_dates("rolling")
    assert len(targets) == 3
    assert (targets[1] - targets[0]).days == 1
    assert (targets[2] - targets[1]).days == 1


def test_completed_open_target_dates_excludes_today_and_future_after_grace():
    targets = _completed_open_target_dates(
        ["2026-06-07", "2026-06-08", "2026-06-09"],
        now=datetime(2026, 6, 8, 8, 30, tzinfo=SFO_TZ),
    )
    assert targets == ["2026-06-07"]


def test_rolling_live_event_targets_include_today_and_next_two_days_before_peak_cutoff():
    now = datetime(2026, 6, 10, 2, 10, tzinfo=SFO_TZ)
    events = [
        _kalshi_event("KXHIGHTSFO-26JUN10", status="active"),
        _kalshi_event("KXHIGHTSFO-26JUN11", status="active"),
        _kalshi_event("KXHIGHTSFO-26JUN12", status="active"),
    ]

    # The rolling scanner now reaches three settlement dates by default
    # (PAPER_ROLLING_TARGETS), widening the candidate universe.
    targets, events_by_target = _rolling_live_event_targets(events, now=now)

    assert targets == [date(2026, 6, 10), date(2026, 6, 11), date(2026, 6, 12)]
    assert events_by_target[date(2026, 6, 10)].event_ticker == "KXHIGHTSFO-26JUN10"
    assert events_by_target[date(2026, 6, 11)].event_ticker == "KXHIGHTSFO-26JUN11"
    assert events_by_target[date(2026, 6, 12)].event_ticker == "KXHIGHTSFO-26JUN12"


def test_rolling_live_event_targets_can_be_capped_to_one_day_after_peak_cutoff():
    now = datetime(2026, 6, 10, 15, 1, tzinfo=SFO_TZ)
    events = [
        _kalshi_event("KXHIGHTSFO-26JUN10", status="active"),
        _kalshi_event("KXHIGHTSFO-26JUN11", status="active"),
        _kalshi_event("KXHIGHTSFO-26JUN12", status="active"),
    ]

    targets, events_by_target = _rolling_live_event_targets(events, now=now, max_targets=1)

    assert targets == [date(2026, 6, 11)]
    assert events_by_target[date(2026, 6, 11)].event_ticker == "KXHIGHTSFO-26JUN11"


def test_rolling_live_event_targets_include_next_two_active_days_after_peak_cutoff():
    now = datetime(2026, 6, 10, 15, 1, tzinfo=SFO_TZ)
    events = [
        _kalshi_event("KXHIGHTSFO-26JUN10", status="active"),
        _kalshi_event("KXHIGHTSFO-26JUN11", status="active"),
        _kalshi_event("KXHIGHTSFO-26JUN12", status="active"),
    ]

    targets, events_by_target = _rolling_live_event_targets(events, now=now)

    assert targets == [date(2026, 6, 11), date(2026, 6, 12)]
    assert events_by_target[date(2026, 6, 11)].event_ticker == "KXHIGHTSFO-26JUN11"
    assert events_by_target[date(2026, 6, 12)].event_ticker == "KXHIGHTSFO-26JUN12"


def test_rolling_live_event_targets_do_not_invent_tomorrow_at_midnight():
    now = datetime(2026, 6, 8, 0, 10, tzinfo=SFO_TZ)
    events = [_kalshi_event("KXHIGHTSFO-26JUN08", status="active")]

    targets, _ = _rolling_live_event_targets(events, now=now)

    assert targets == [date(2026, 6, 8)]


def test_settlement_today_uses_fixed_pst_after_civil_midnight():
    # 00:30 PDT June 8 is 23:30 PST June 7: settlement day 7 is still running.
    civil_after_midnight = datetime(2026, 6, 8, 0, 30, tzinfo=SFO_TZ)
    assert settlement_today(civil_after_midnight) == date(2026, 6, 7)
    assert settlement_today(datetime(2026, 6, 8, 1, 30, tzinfo=SFO_TZ)) == date(2026, 6, 8)


def test_completed_open_target_dates_keep_running_settlement_day_open():
    targets = _completed_open_target_dates(
        ["2026-06-07", "2026-06-08"],
        now=datetime(2026, 6, 8, 0, 30, tzinfo=SFO_TZ),
    )
    assert targets == []


def test_rolling_live_event_targets_skip_almost_settled_day_after_civil_midnight():
    # 23:40 PST June 7: do not open new same-day positions in the last
    # settlement hour; the scanner should already be on June 8.
    now = datetime(2026, 6, 8, 0, 40, tzinfo=SFO_TZ)
    events = [
        _kalshi_event("KXHIGHTSFO-26JUN07", status="active"),
        _kalshi_event("KXHIGHTSFO-26JUN08", status="active"),
    ]

    targets, _ = _rolling_live_event_targets(events, now=now)

    assert targets == [date(2026, 6, 8)]


def _kalshi_event(event_ticker: str, *, status: str) -> EventSnapshot:
    return EventSnapshot.from_kalshi(
        {
            "event_ticker": event_ticker,
            "title": f"Highest temperature in San Francisco on {event_ticker}",
            "markets": [
                {
                    "ticker": f"{event_ticker}-B66.5",
                    "event_ticker": event_ticker,
                    "title": "High temperature 66 to 67",
                    "yes_sub_title": "66 to 67",
                    "strike_type": "between",
                    "floor_strike": "66",
                    "cap_strike": "67",
                    "yes_bid_dollars": "0.20",
                    "yes_ask_dollars": "0.30",
                    "no_bid_dollars": "0.65",
                    "no_ask_dollars": "0.75",
                    "yes_bid_size_fp": "10",
                    "yes_ask_size_fp": "10",
                    "status": status,
                }
            ],
        }
    )
