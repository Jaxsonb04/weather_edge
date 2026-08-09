"""Archived profiles publish their own era curve, not the shared window.

The shared `days` window is a trailing view keyed to today. An archived
profile stopped trading before that window opened, so reprojecting it onto the
window resolves nothing every day and its curve degenerates to a horizontal
line pinned at its all-time total. These tests lock the era behaviour in.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sfo_kalshi_quant.summary import _profile_era_summaries
from sfo_kalshi_quant.strategy_lab.profiles import _profile_views


ARCHIVED = "research-target-v4"
ACTIVE = "live"


def _opened(profile: str, at: str, *, contracts: float = 10.0) -> dict:
    return {
        "risk_profile": profile,
        "filled_at": at,
        "created_at": at,
        "status": "PAPER_SETTLED",
        "contracts": contracts,
        "cost_per_contract": 0.5,
    }


def _lot(profile: str, at: str, pnl: float, *, contracts: float = 10.0) -> dict:
    return {
        "risk_profile": profile,
        "closed_at": None,
        "settled_at": at,
        "realized_pnl": pnl,
        "contracts": contracts,
        "cost_per_contract": 0.5,
    }


def _terminal(profile: str, at: str, outcome: str) -> dict:
    return {
        "risk_profile": profile,
        "latest_resolved_at": at,
        "closed_at": None,
        "settled_at": at,
        "status": "PAPER_SETTLED",
        "logical_outcome": outcome,
    }


# Two resolutions a day apart, well outside any plausible trailing window.
D1 = "2026-01-05T20:00:00+00:00"
D2 = "2026-01-06T20:00:00+00:00"
D3 = "2026-01-08T20:00:00+00:00"


def _era_inputs(profile: str = ARCHIVED):
    return {
        "valid_position_rows": [
            _opened(profile, D1),
            _opened(profile, D2),
            _opened(profile, D3),
        ],
        "resolved_lots": [
            _lot(profile, D1, 4.0),
            _lot(profile, D2, -1.5),
            _lot(profile, D3, 2.5),
        ],
        "terminal_rows": [
            _terminal(profile, D1, "win"),
            _terminal(profile, D2, "loss"),
            _terminal(profile, D3, "win"),
        ],
    }


def test_era_spans_only_the_profiles_own_active_life():
    eras = _profile_era_summaries(**_era_inputs())

    era = eras[ARCHIVED]
    assert era["window_start"] == "2026-01-05"
    assert era["window_end"] == "2026-01-08"
    # Dense across the gap day so a quiet stretch reads as flat, not skipped.
    assert era["window_days"] == 4
    assert [day["date"] for day in era["days"]] == [
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
        "2026-01-08",
    ]


def test_era_curve_moves_and_closes_on_the_all_time_total():
    era = _profile_era_summaries(**_era_inputs())[ARCHIVED]

    cumulative = [day["cumulative_realized"] for day in era["days"]]
    assert cumulative == [4.0, 2.5, 2.5, 5.0]
    # The whole point: an archived curve must not be a single repeated value.
    assert len(set(cumulative)) > 1
    assert era["totals"]["realized_pnl"] == 5.0
    assert era["totals"]["wins"] == 2
    assert era["totals"]["losses"] == 1


def test_era_omits_profiles_that_never_resolved_anything():
    inputs = _era_inputs()
    inputs["resolved_lots"] = []
    inputs["terminal_rows"] = []

    assert _profile_era_summaries(**inputs) == {}


def test_era_ignores_the_unknown_profile_sentinel():
    eras = _profile_era_summaries(**_era_inputs(profile="unknown"))

    assert eras == {}


def test_truncated_era_opens_at_the_level_already_reached(monkeypatch):
    """Capping the span must shift the curve's window, never its level."""

    from sfo_kalshi_quant import summary as summary_module

    # Cap to the final two days, dropping the first resolution from the view.
    monkeypatch.setattr(summary_module, "_MAX_ERA_DAYS", 2)
    era = summary_module._profile_era_summaries(**_era_inputs())[ARCHIVED]

    assert [day["date"] for day in era["days"]] == ["2026-01-07", "2026-01-08"]
    # The dropped 2026-01-05 win (+4.0) and 2026-01-06 loss (-1.5) still set
    # the opening level, so the tail reads 2.5 -> 5.0 rather than 0.0 -> 2.5.
    assert [day["cumulative_realized"] for day in era["days"]] == [2.5, 5.0]


def _daily_summary(eras: dict) -> dict:
    return {
        "available": True,
        "schema_version": 2,
        "window_days": 7,
        "window_start": "2026-03-01",
        "window_end": "2026-03-07",
        "days": [
            {"date": "2026-03-0%d" % n, "profiles": {}} for n in range(1, 8)
        ],
        "profiles": [],
        "profile_eras": eras,
    }


def _paper(profile: str, realized: float | None) -> dict:
    row = {"risk_profile": profile, "open_positions": 0, "open_risk": 0.0}
    if realized is not None:
        row["realized_pnl"] = realized
    return {
        "profiles": [row],
        "closed_positions": [],
        "open_positions": [],
        "pending_limit_orders": [],
        "recent_monitor_actions": [],
    }


def _view(views: list, profile: str) -> dict:
    return next(row for row in views if row["risk_profile"] == profile)


def test_archived_profile_view_uses_its_era_not_the_shared_window():
    eras = _profile_era_summaries(**_era_inputs())
    views = _profile_views(
        daily_summary=_daily_summary(eras),
        paper=_paper(ARCHIVED, 5.0),
        signal_quality={},
    )

    daily = _view(views, ARCHIVED)["daily_summary"]
    assert daily["window_basis"] == "profile_era"
    assert daily["window_start"] == "2026-01-05"
    assert daily["window_end"] == "2026-01-08"
    cumulative = [day["cumulative_realized"] for day in daily["days"]]
    assert len(set(cumulative)) > 1
    # An era covers the profile's whole life, so its window P&L is its total.
    assert daily["totals"]["realized_pnl"] == 5.0
    assert daily["totals"]["all_time_attributed_pnl"] == 5.0


def test_active_profile_view_keeps_the_shared_rolling_window():
    eras = _profile_era_summaries(**_era_inputs(profile=ACTIVE))
    views = _profile_views(
        daily_summary=_daily_summary(eras),
        paper=_paper(ACTIVE, 5.0),
        signal_quality={},
    )

    daily = _view(views, ACTIVE)["daily_summary"]
    assert "window_basis" not in daily
    assert daily["window_start"] == "2026-03-01"
    assert daily["window_end"] == "2026-03-07"
    assert len(daily["days"]) == 7


def test_era_terminal_point_reconciles_to_the_published_paper_total():
    eras = _profile_era_summaries(**_era_inputs())
    # Order-level truth disagrees a cent with the sum of rounded daily rows.
    views = _profile_views(
        daily_summary=_daily_summary(eras),
        paper=_paper(ARCHIVED, 5.01),
        signal_quality={},
    )

    daily = _view(views, ARCHIVED)["daily_summary"]
    assert daily["days"][-1]["cumulative_realized"] == 5.01
    assert daily["days"][-1]["closing_attributed_pnl"] == 5.01
    assert daily["totals"]["realized_pnl"] == 5.01


def test_era_without_a_published_paper_total_keeps_its_own_arithmetic():
    """A missing paper total must not pin the curve to a fabricated zero."""

    eras = _profile_era_summaries(**_era_inputs())
    views = _profile_views(
        daily_summary=_daily_summary(eras),
        paper=_paper(ARCHIVED, None),
        signal_quality={},
    )

    daily = _view(views, ARCHIVED)["daily_summary"]
    assert daily["days"][-1]["cumulative_realized"] == 5.0
    assert daily["totals"]["all_time_attributed_pnl"] == 5.0


def test_archived_profile_stays_visible_when_only_its_era_names_it():
    """The window-scoped lists cannot name a profile that resolved nothing."""

    eras = _profile_era_summaries(**_era_inputs())
    views = _profile_views(
        daily_summary=_daily_summary(eras),
        paper={
            "profiles": [],
            "closed_positions": [],
            "open_positions": [],
            "pending_limit_orders": [],
            "recent_monitor_actions": [],
        },
        signal_quality={},
    )

    assert ARCHIVED in {row["risk_profile"] for row in views}
