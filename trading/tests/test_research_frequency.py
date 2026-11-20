"""W2 research frequency (2026-09-13): seller-flow scan order and the resting
TTL table (the v8 book is taker-only since 2026-11-12, so its stale-quote guard
was removed). Research-only --
every test here also pins that the live book is untouched. Section 3 pins why
a scan-lock change was reverted: systemd, not the shell lock, decides what
happens to a tick that a slow scan overruns."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from sfo_kalshi_quant._cli import scan as scan_module
from sfo_kalshi_quant.cities import CITIES, CITY_BY_SLUG, get_city
from sfo_kalshi_quant.cli import build_parser, cmd_portfolio_scan
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.research_entry_risk import (
    DEFAULT_RESTING_ORDER_TTL_MINUTES,
    RESEARCH_ENTRY_RISK_VERSION,
    TARGET_DAY_AHEAD_RESTING_ORDER_TTL_MINUTES,
    resting_order_ttl_minutes,
)
from sfo_kalshi_quant.research_policy import (
    MOTION_POLICY,
    RESEARCH_SCAN_CITY_ORDER,
    TARGET_POLICY,
    research_scan_city_rank,
)
from sfo_kalshi_quant.replay import _row_ttl_minutes
from test_expired_requote import TARGET as LIVE_TARGET
from test_expired_requote import _limit_trader, _resting_decision


# --------------------------------------------------------------------------
# 1. Seller-flow scan order
# --------------------------------------------------------------------------


_MEASURED_DAY_AHEAD_NO_SELLER_FLOW = {
    "lax": 14132, "nyc": 4482, "mia": 3122, "aus": 2686, "chi": 2476,
    "dal": 1943, "sfo": 1794, "okc": 1753, "den": 1638, "lv": 1530,
    "atl": 1427, "phl": 1336, "bos": 1313, "sea": 1217, "hou": 1005,
    "phx": 978, "satx": 786, "min": 758, "nola": 721, "dc": 680,
}


def test_research_scan_order_ranks_every_registry_city_by_measured_seller_flow() -> None:
    # Day-ahead NO-seller contracts per event day on the public tape
    # (taker-YES trades dated before the event date, events 2026-09-05..13);
    # the table above is the evidence recorded beside RESEARCH_SCAN_CITY_ORDER.
    assert len(RESEARCH_SCAN_CITY_ORDER) == len(set(RESEARCH_SCAN_CITY_ORDER))
    # Every registry city is measured and listed. research_scan_city_rank still
    # appends a city added later without a measurement (tests below).
    assert set(_MEASURED_DAY_AHEAD_NO_SELLER_FLOW) == set(CITY_BY_SLUG)
    assert set(RESEARCH_SCAN_CITY_ORDER) == set(CITY_BY_SLUG)
    assert len(RESEARCH_SCAN_CITY_ORDER) == len(CITIES) == 20
    assert list(RESEARCH_SCAN_CITY_ORDER) == sorted(
        _MEASURED_DAY_AHEAD_NO_SELLER_FLOW,
        key=lambda slug: -_MEASURED_DAY_AHEAD_NO_SELLER_FLOW[slug],
    )
    # The measured day-ahead NO-seller flow leaders come first.
    assert RESEARCH_SCAN_CITY_ORDER[:3] == ("lax", "nyc", "mia")
    # ...and the registry's first city (MIA) is no longer scanned first.
    assert CITIES[0].slug == "mia"
    assert RESEARCH_SCAN_CITY_ORDER[0] != CITIES[0].slug


def test_unlisted_city_ranks_last_and_is_never_dropped() -> None:
    assert research_scan_city_rank("LAX") == 0
    assert research_scan_city_rank("zzz") == len(RESEARCH_SCAN_CITY_ORDER)
    future = replace(get_city("sfo"), slug="zzz", name="Future City")
    ordered = scan_module._scan_cities_for_profile((future, *CITIES), "research")
    registry_unlisted = [
        city.slug for city in CITIES if city.slug not in RESEARCH_SCAN_CITY_ORDER
    ]
    assert [city.slug for city in ordered] == [
        *RESEARCH_SCAN_CITY_ORDER,
        "zzz",
        *registry_unlisted,
    ]
    # The live profile keeps whatever order it was handed, future city included.
    assert scan_module._scan_cities_for_profile((future, *CITIES), "live") == (
        future,
        *CITIES,
    )


def _scanned_city_order(profile: str, cities: tuple = CITIES) -> tuple[list[str], list]:
    args = build_parser().parse_args(["--risk-profile", profile, "portfolio-scan"])
    target = date(2026, 9, 14)
    adapter = Mock()
    adapter.load_calibration_outcomes.return_value = [object()] * 30
    adapter.load_emos_mu_sigma.return_value = {}
    with (
        patch("sfo_kalshi_quant.cli._cities_for_args", return_value=cities),
        patch(
            "sfo_kalshi_quant.cli._resolve_analysis_targets",
            return_value=([target], {}),
        ),
        patch("sfo_kalshi_quant.cli.SfoForecasterAdapter", return_value=adapter),
        patch("sfo_kalshi_quant.cli.ResidualCalibrator", return_value=object()),
        patch("sfo_kalshi_quant.cli.KalshiPublicClient"),
        patch("sfo_kalshi_quant.cli.PaperStore"),
        patch("sfo_kalshi_quant.cli._build_sizing_model", return_value=None),
        patch("sfo_kalshi_quant.cli._portfolio_scan_one_target") as scan_target,
    ):
        code = cmd_portfolio_scan(args)
    assert code == 0
    slugs = [call_.kwargs["city"].slug for call_ in scan_target.call_args_list]
    return slugs, scan_target.call_args_list


def test_live_portfolio_scan_keeps_registry_order() -> None:
    slugs, calls = _scanned_city_order("live")
    assert slugs == [city.slug for city in CITIES]
    assert slugs[0] == "mia"
    # Snapshot recording is a per-invocation flag the runner sets only for the
    # second (research) process; the live process' args are passed through
    # untouched, so its context snapshots keep being recorded.
    assert all(
        getattr(call_.args[0], "skip_context_snapshots", False) is False
        for call_ in calls
    )


def test_research_portfolio_scan_follows_seller_flow_order() -> None:
    slugs, _ = _scanned_city_order("research")
    unlisted = [city.slug for city in CITIES if city.slug not in RESEARCH_SCAN_CITY_ORDER]
    assert slugs == [*RESEARCH_SCAN_CITY_ORDER, *unlisted]
    assert slugs[0] == "lax"
    assert sorted(slugs) == sorted(city.slug for city in CITIES)


# feat/five-more-high-cities appends these to cities.CITIES in this order (Las
# Vegas, Minneapolis, San Antonio, New Orleans, Washington DC). They are
# ranked in RESEARCH_SCAN_CITY_ORDER by the same measured seller flow as every
# other city, and the research scan must reach each one at that rank.
_FIVE_NEW_CITY_SLUGS = ("lv", "min", "satx", "nola", "dc")


def _registry_with_five_new_cities() -> tuple:
    """The 20-city registry, whether or not that branch is integrated yet."""

    template = get_city("sfo")
    existing = tuple(city for city in CITIES if city.slug not in _FIVE_NEW_CITY_SLUGS)
    new = tuple(
        replace(
            template,
            slug=slug,
            name=f"New city {slug}",
            series_ticker=f"KXHIGHT{slug.upper()}",
        )
        for slug in _FIVE_NEW_CITY_SLUGS
    )
    return (*existing, *new)


def test_research_scan_ranks_the_five_new_cities_by_measured_flow() -> None:
    registry = _registry_with_five_new_cities()
    assert len(registry) == 20
    for slug in _FIVE_NEW_CITY_SLUGS:
        assert slug in RESEARCH_SCAN_CITY_ORDER
        assert research_scan_city_rank(slug) == RESEARCH_SCAN_CITY_ORDER.index(slug)
        assert research_scan_city_rank(slug) < len(RESEARCH_SCAN_CITY_ORDER)

    ordered = scan_module._scan_cities_for_profile(registry, "research")

    # Nothing dropped, nothing duplicated, every city at its measured rank.
    assert [city.slug for city in ordered] == list(RESEARCH_SCAN_CITY_ORDER)
    assert sorted(city.slug for city in ordered) == sorted(city.slug for city in registry)
    # A PAPER_CITIES subset keeps the same contract.
    by_slug = {city.slug: city for city in registry}
    subset = (by_slug["dc"], by_slug["atl"], by_slug["lv"], by_slug["lax"])
    assert [
        city.slug for city in scan_module._scan_cities_for_profile(subset, "research")
    ] == ['lax', 'lv', 'atl', 'dc']
    # The live profile keeps the registry order untouched.
    assert scan_module._scan_cities_for_profile(registry, "live") == registry


def test_research_portfolio_scan_reaches_the_five_new_cities() -> None:
    registry = _registry_with_five_new_cities()

    research_slugs, _ = _scanned_city_order("research", cities=registry)
    live_slugs, _ = _scanned_city_order("live", cities=registry)

    assert research_slugs == list(RESEARCH_SCAN_CITY_ORDER)
    assert set(_FIVE_NEW_CITY_SLUGS) <= set(research_slugs)
    assert live_slugs == [city.slug for city in registry]


# --------------------------------------------------------------------------
# 2. Resting TTL: 30 minutes for target-sleeve day-ahead quotes, 15 otherwise
# --------------------------------------------------------------------------


def test_entry_risk_version_is_the_market_signal_release() -> None:
    assert RESEARCH_ENTRY_RISK_VERSION == "research-entry-risk-v4-market-signal-2026-11-12"


@pytest.mark.parametrize(
    ("account_id", "lead_bucket", "expected"),
    [
        (TARGET_POLICY.account_id, "day-ahead", 30),
        (TARGET_POLICY.account_id, "same-day", 15),
        (MOTION_POLICY.account_id, "day-ahead", 15),
        ("paper-live-stability-v1", "day-ahead", 15),
        ("paper-live-stability-v1", None, 15),
        (None, None, 15),
    ],
)
def test_resting_order_ttl_selection(account_id, lead_bucket, expected) -> None:
    assert resting_order_ttl_minutes(account_id=account_id, lead_bucket=lead_bucket) == expected
    assert DEFAULT_RESTING_ORDER_TTL_MINUTES == 15
    assert TARGET_DAY_AHEAD_RESTING_ORDER_TTL_MINUTES == 30


def _rest_minutes(row) -> float:
    created = datetime.fromisoformat(str(row["created_at"]))
    expires = datetime.fromisoformat(str(row["expires_at"]))
    return (expires - created).total_seconds() / 60.0


def test_live_resting_order_still_rests_fifteen_minutes() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "ttl-live.db")
        placed_ids = _limit_trader(store).place_approved(LIVE_TARGET, [_resting_decision()])
        assert len(placed_ids) == 1
        row = store.paper_order(placed_ids[0])
        assert row["status"] == "PAPER_LIMIT_RESTING"
        assert row["account_id"] != TARGET_POLICY.account_id
        assert _rest_minutes(row) == pytest.approx(15.0)
        placed = datetime.fromisoformat(str(row["created_at"]))
        assert _row_ttl_minutes(row, placed) == 15


def test_replay_row_ttl_falls_back_to_fifteen_without_a_usable_expiry() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    placed = datetime(2026, 9, 13, 14, 0, tzinfo=UTC)
    no_expiry = conn.execute("SELECT NULL AS expires_at, 1 AS id").fetchone()
    assert _row_ttl_minutes(no_expiry, placed) == 15
    thirty = conn.execute(
        "SELECT ? AS expires_at, 1 AS id",
        ((placed + timedelta(minutes=30)).isoformat(),),
    ).fetchone()
    assert _row_ttl_minutes(thirty, placed) == 30
    backwards = conn.execute(
        "SELECT ? AS expires_at, 1 AS id",
        ((placed - timedelta(minutes=5)).isoformat(),),
    ).fetchone()
    assert _row_ttl_minutes(backwards, placed) == 15


def test_research_replay_rests_day_ahead_cases_thirty_minutes() -> None:
    from test_research_replay import _case, _identity
    from sfo_kalshi_quant.research_replay import case_replay_payload, replay_case_candidate

    day_ahead = replay_case_candidate(_case(lead_days=1), _identity(mu=70.0, sigma=3.0))
    assert day_ahead.available
    priced = [t for t in day_ahead.tickers if t.limit_price is not None]
    assert priced, "fixture must replay at least one priced order"
    assert {t.ttl_minutes for t in priced} == {30}
    payload = case_replay_payload(day_ahead)
    assert {t["ttl_minutes"] for t in payload["tickers"] if t["limit_price"] is not None} == {30}
    json.dumps(payload, allow_nan=False)

    same_day = replay_case_candidate(_case(lead_days=0), _identity(mu=70.0, sigma=3.0))
    same_day_priced = [t for t in same_day.tickers if t.limit_price is not None]
    assert same_day_priced
    assert {t.ttl_minutes for t in same_day_priced} == {15}


# --------------------------------------------------------------------------
# 3. Scan tick overruns: systemd catches the tick up; the shell lock is not
#    involved (2026-09-13 correction -- a bounded lock wait was reverted)
# --------------------------------------------------------------------------


_AWS_DIR = Path(__file__).resolve().parents[1] / "deploy" / "aws"
_SYSTEMD_DIR = _AWS_DIR / "systemd"
_MONOTONIC_TIMER_KEYS = {
    "OnActiveSec",
    "OnBootSec",
    "OnStartupSec",
    "OnUnitActiveSec",
    "OnUnitInactiveSec",
}


def _unit_directives(name: str) -> list[str]:
    return [
        line.strip()
        for line in (_SYSTEMD_DIR / name).read_text().splitlines()
        if line.strip() and not line.strip().startswith(("#", ";"))
    ]


def test_scan_tick_overrun_is_caught_up_by_systemd_not_the_shell_lock() -> None:
    """A scan that overruns its 5-minute slot delays the next tick; it does
    not drop it -- and the runner's flock plays no part in that.

    systemd never starts this Type=oneshot service twice, and while a
    timer-started run is active the timer is not armed (timer_dispatch
    ignores an elapse outside TIMER_WAITING). When the run exits,
    timer_trigger_notify re-enters timer_enter_waiting, which computes the
    next OnCalendar elapse from last_trigger, so the elapse that passed during
    the overrun (the 14:00Z listing tick behind a slow 13:55 scan) is already
    due and starts at once. Reproduced on Ubuntu 24.04 / systemd 255.4, the
    production OS: a 20 s calendar timer driving a 25 s oneshot started runs
    back-to-back 25 s apart, not 40 s apart. Timer-driven runs therefore never
    contend on the runner's lock, so a longer lock wait could not rescue a
    tick. This pins the unit shape that catch-up depends on.
    """

    timer = _unit_directives("sfo-kalshi-paper-scan.timer")
    service = _unit_directives("sfo-kalshi-paper-scan.service.in")

    # Calendar-based, so the next elapse is computed from the last trigger; no
    # monotonic base that would re-time the schedule from activation instead.
    assert len([line for line in timer if line.startswith("OnCalendar=")]) == 1
    assert not [
        line for line in timer if line.split("=", 1)[0] in _MONOTONIC_TIMER_KEYS
    ]
    assert "Unit=sfo-kalshi-paper-scan.service" in timer

    # One oneshot activation per trigger that goes inactive when the runner
    # exits -- that deactivation is what re-arms the timer. RemainAfterExit=yes
    # would leave the unit active and silently stop every later tick.
    assert "Type=oneshot" in service
    assert not [
        line for line in service if line.lower().startswith("remainafterexit=")
    ]
    assert [line for line in service if line.startswith("ExecStart=")] == [
        "ExecStart=/usr/bin/env bash __TRADING_DIR__/deploy/aws/run_paper_scan_profiles.sh"
    ]

    # The lock stays skip-at-once: it only ever meets an out-of-band run.
    runner = (_AWS_DIR / "run_paper_scan_profiles.sh").read_text()
    assert runner.count("flock -n 9") == 1
    assert "flock -w" not in runner
    assert "SFO_PAPER_SCAN_LOCK_WAIT_SECONDS" not in runner
    example_env = (_AWS_DIR / "sfo-weather.env.example").read_text()
    assert "SFO_PAPER_SCAN_LOCK_WAIT_SECONDS" not in example_env
