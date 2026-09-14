"""W2 research frequency (2026-09-13): seller-flow scan order and a 30-minute
day-ahead research rest with a stale-quote cancel guard. Research-only --
every test here also pins that the live book is untouched. Section 3 pins why
a scan-lock change was reverted: systemd, not the shell lock, decides what
happens to a tick that a slow scan overruns."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from sfo_kalshi_quant._cli import scan as scan_module
from sfo_kalshi_quant.cities import CITIES, CITY_BY_SLUG, get_city
from sfo_kalshi_quant.cli import build_parser, cmd_portfolio_scan
from sfo_kalshi_quant.config import strategy_config_for_profile
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.maker_fills import MAKER_TAPE_RECONCILIATION_GRACE_SECONDS
from sfo_kalshi_quant.paper import PaperTrader
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
from test_research_sleeves import (
    _atomic_decision,
    _fixed_research_clock,
    _linked_admission,
)
from test_target_execution_capacity import _candidate


# --------------------------------------------------------------------------
# 1. Seller-flow scan order
# --------------------------------------------------------------------------


def test_research_scan_order_lists_only_registry_cities_without_duplicates() -> None:
    assert len(RESEARCH_SCAN_CITY_ORDER) == len(set(RESEARCH_SCAN_CITY_ORDER))
    # Every listed slug is a real registry city. The registry may grow past
    # this list -- an unlisted city is appended, never dropped (tests below)
    # -- so this is a subset check, not equality.
    assert set(RESEARCH_SCAN_CITY_ORDER) <= set(CITY_BY_SLUG)
    assert len(RESEARCH_SCAN_CITY_ORDER) == 15
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
# Vegas, Minneapolis, San Antonio, New Orleans, Washington DC). They have no
# measured seller flow, so they are deliberately absent from
# RESEARCH_SCAN_CITY_ORDER -- and the research scan must still reach each one.
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


def test_research_scan_appends_configured_cities_missing_from_the_order() -> None:
    registry = _registry_with_five_new_cities()
    assert len(registry) == 20
    for slug in _FIVE_NEW_CITY_SLUGS:
        assert slug not in RESEARCH_SCAN_CITY_ORDER
        assert research_scan_city_rank(slug) == len(RESEARCH_SCAN_CITY_ORDER)

    ordered = scan_module._scan_cities_for_profile(registry, "research")

    # Nothing dropped, nothing duplicated: listed cities in seller-flow order,
    # then every unlisted configured city in its registry order.
    assert [city.slug for city in ordered] == [
        *RESEARCH_SCAN_CITY_ORDER,
        *_FIVE_NEW_CITY_SLUGS,
    ]
    assert sorted(city.slug for city in ordered) == sorted(city.slug for city in registry)
    # A PAPER_CITIES subset keeps the same contract.
    by_slug = {city.slug: city for city in registry}
    subset = (by_slug["dc"], by_slug["atl"], by_slug["lv"], by_slug["lax"])
    assert [
        city.slug for city in scan_module._scan_cities_for_profile(subset, "research")
    ] == ["lax", "atl", "dc", "lv"]
    # The live profile keeps the registry order untouched.
    assert scan_module._scan_cities_for_profile(registry, "live") == registry


def test_research_portfolio_scan_reaches_the_five_new_cities() -> None:
    registry = _registry_with_five_new_cities()

    research_slugs, _ = _scanned_city_order("research", cities=registry)
    live_slugs, _ = _scanned_city_order("live", cities=registry)

    assert research_slugs == [*RESEARCH_SCAN_CITY_ORDER, *_FIVE_NEW_CITY_SLUGS]
    assert live_slugs == [city.slug for city in registry]


# --------------------------------------------------------------------------
# 2. Resting TTL: 30 minutes for target-sleeve day-ahead quotes, 15 otherwise
# --------------------------------------------------------------------------


def test_entry_risk_version_is_the_scaling_release() -> None:
    assert RESEARCH_ENTRY_RISK_VERSION == "research-entry-risk-v3-scaling-2026-09-13"


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


def _admit_resting_target_order(store: PaperStore, suffix: str, **decision_changes):
    decision = _atomic_decision(f"KXHIGHTSFO-W2-{suffix}", contracts=1.0, resting=True)
    if decision_changes:
        decision = replace(decision, **decision_changes)
    admission = _linked_admission(store, TARGET_POLICY, f"w2-{suffix}", decision)
    order_id = store.record_research_order_atomic(
        "2026-07-19",
        decision,
        admission=admission,
        strategy_config=strategy_config_for_profile("research"),
    )
    assert order_id is not None
    row = store.paper_order(order_id)
    assert row is not None and row["status"] == "PAPER_LIMIT_RESTING"
    return order_id, row, decision


def test_target_day_ahead_research_order_rests_thirty_minutes(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "ttl-research.db", research_clock=_fixed_research_clock)
    _, row, _ = _admit_resting_target_order(store, "TTL")
    assert row["account_id"] == TARGET_POLICY.account_id
    assert row["lead_bucket"] == "day-ahead"
    assert _rest_minutes(row) == pytest.approx(30.0)
    # The DB replay rebuilds the order with the TTL the row actually carried.
    placed = datetime.fromisoformat(str(row["created_at"]))
    assert _row_ttl_minutes(row, placed) == 30


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
# 2b. Stale-quote guard
# --------------------------------------------------------------------------


def _research_trader(store: PaperStore) -> PaperTrader:
    return PaperTrader(
        store,
        strategy_config_for_profile("research"),
        risk_profile="research",
        entry_mode="limit",
    )


def _ledger_events(store: PaperStore, order_id: int) -> list[str]:
    with sqlite3.connect(store.db_path) as conn:
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT event_type FROM paper_account_ledger WHERE order_id=? ORDER BY id",
                (order_id,),
            )
        ]


def _backdate_resting_order(
    store: PaperStore, order_id: int, *, placed_minutes_ago: float
) -> datetime:
    placed = datetime.now(UTC) - timedelta(minutes=placed_minutes_ago)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE paper_orders SET created_at=?, expires_at=? WHERE id=?",
            (
                placed.isoformat(),
                (placed + timedelta(minutes=30)).isoformat(),
                order_id,
            ),
        )
    return placed


def _trade_through(row, *, trade_id: str, at: datetime) -> dict[str, object]:
    """A public trade one cent through the resting quote, on its maker side."""

    side = str(row["side"] or "YES").upper()
    limit = float(
        row["limit_price"] if row["limit_price"] is not None else row["entry_price"]
    )
    side_price = round(max(0.01, limit - 0.01), 2)
    yes_price = side_price if side == "YES" else round(1.0 - side_price, 2)
    return {
        "trade_id": trade_id,
        "created_time": at.isoformat(),
        # An ask-side taker fills resting YES bids; a bid-side taker fills NO.
        "taker_book_side": "ask" if side == "YES" else "bid",
        "yes_price_dollars": f"{yes_price:.2f}",
        "no_price_dollars": f"{1.0 - yes_price:.2f}",
        "count_fp": "500.00",
    }


def test_stale_pull_keeps_a_trade_through_that_preceded_it_creditable(
    tmp_path: Path,
) -> None:
    """Release review, cross-track HIGH: a direct cancel erased this fill.

    The quote rested 10 minutes; the tape traded through it 5 minutes ago; the
    monitor (a separate 2-minute timer that waits out a 5-minute ingestion
    grace) had not credited it when the scan's stale guard fired. The guard
    must not make that fill uncreditable -- apply_maker_trade_batch never
    revisits a terminal row, and a pull fires exactly when sellers hit.
    """

    store = PaperStore(tmp_path / "pull.db", research_clock=_fixed_research_clock)
    order_id, row, decision = _admit_resting_target_order(store, "PULL")
    _backdate_resting_order(store, order_id, placed_minutes_ago=10)
    stale = replace(decision, probability_lcb=float(row["cost_per_contract"]) - 0.05)

    pulled = _research_trader(store).cancel_stale_research_resting_orders(
        "2026-07-19", [stale]
    )

    assert pulled == [order_id]
    after_pull = store.paper_order(order_id)
    assert after_pull["status"] == "PAPER_LIMIT_RESTING"
    assert float(after_pull["reserved_cost"]) > 0
    before_pull = _trade_through(
        row, trade_id="before-pull", at=datetime.now(UTC) - timedelta(minutes=5)
    )
    store.apply_maker_trade_batch(decision.ticker, [before_pull])

    filled = store.paper_order(order_id)
    assert float(filled["filled_contracts"] or 0.0) > 0.0
    assert filled["status"] == "PAPER_FILLED"


def test_stale_pull_rejects_later_tape_and_expires_with_its_reason_once_reconciled(
    tmp_path: Path,
) -> None:
    store = PaperStore(tmp_path / "stale.db", research_clock=_fixed_research_clock)
    order_id, row, decision = _admit_resting_target_order(store, "STALE")
    _backdate_resting_order(store, order_id, placed_minutes_ago=10)
    assert float(row["reserved_cost"]) > 0
    # The forecast moved: the fresh LCB no longer covers the resting after-fee cost.
    stale = replace(decision, probability_lcb=float(row["cost_per_contract"]) - 0.05)
    trader = _research_trader(store)

    assert trader.cancel_stale_research_resting_orders("2026-07-19", [stale]) == [order_id]

    pulled = store.paper_order(order_id)
    request = json.loads(pulled["outcome_diagnostics_json"])["cancel_request"]
    assert request["reason"].startswith("stale research quote")
    # The quote now ends at the pull instant; its 30-minute expiry is kept for audit.
    assert pulled["expires_at"] == request["requested_at"]
    assert request["original_expires_at"] != pulled["expires_at"]
    # A later tick does not move the first pull instant.
    assert trader.cancel_stale_research_resting_orders("2026-07-19", [stale]) == []
    assert store.paper_order(order_id)["expires_at"] == request["requested_at"]

    pulled_at = datetime.fromisoformat(request["requested_at"])
    after_pull = _trade_through(
        row, trade_id="after-pull", at=pulled_at + timedelta(seconds=30)
    )
    store.apply_maker_trade_batch(decision.ticker, [after_pull])
    assert float(store.paper_order(order_id)["filled_contracts"] or 0.0) == 0.0

    later = (pulled_at + timedelta(minutes=10)).isoformat()
    short_watermark = (
        pulled_at + timedelta(seconds=MAKER_TAPE_RECONCILIATION_GRACE_SECONDS - 1)
    ).isoformat()
    # Tape not yet complete through the pull: the remainder stays reconcilable.
    assert (
        store.expire_stale_resting_orders(
            now=later, reconciled_through_by_ticker={decision.ticker: short_watermark}
        )
        == 0
    )
    assert store.paper_order(order_id)["status"] == "PAPER_LIMIT_RESTING"

    watermark = (
        pulled_at + timedelta(seconds=MAKER_TAPE_RECONCILIATION_GRACE_SECONDS)
    ).isoformat()
    assert (
        store.expire_stale_resting_orders(
            now=later, reconciled_through_by_ticker={decision.ticker: watermark}
        )
        == 1
    )
    after = store.paper_order(order_id)
    assert after["status"] == "PAPER_EXPIRED"
    assert float(after["reserved_cost"] or 0.0) == 0.0
    assert float(after["remaining_contracts"] or 0.0) == 0.0
    diagnostics = json.loads(after["outcome_diagnostics_json"])
    assert diagnostics["event"] == "cancellation"
    assert diagnostics["reason"].startswith("stale research quote")
    assert diagnostics["tape_reconciled_through"] == watermark
    assert diagnostics["cancel_request"]["original_expires_at"] == (
        request["original_expires_at"]
    )
    assert "RESERVATION_RELEASE" in _ledger_events(store, order_id)
    # An expired quote must not block a fresh re-quote on the same market/side
    # (existing entries_for_market_side contract), so the market is free again.
    assert store.entries_for_market_side("2026-07-19", decision.ticker, "NO") == 0


def test_unpulled_resting_quote_still_expires_as_a_ttl_expiry(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "ttl.db", research_clock=_fixed_research_clock)
    order_id, _row, decision = _admit_resting_target_order(store, "TTLONLY")
    placed = _backdate_resting_order(store, order_id, placed_minutes_ago=45)
    expiry = placed + timedelta(minutes=30)
    watermark = (
        expiry + timedelta(seconds=MAKER_TAPE_RECONCILIATION_GRACE_SECONDS)
    ).isoformat()

    assert (
        store.expire_stale_resting_orders(
            now=datetime.now(UTC).isoformat(),
            reconciled_through_by_ticker={decision.ticker: watermark},
        )
        == 1
    )
    diagnostics = json.loads(store.paper_order(order_id)["outcome_diagnostics_json"])
    assert diagnostics["reason"] == "maker TTL expired"
    assert "cancel_request" not in diagnostics


def test_stale_pull_never_ends_a_quote_before_it_was_placed(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "early.db", research_clock=_fixed_research_clock)
    order_id, row, _decision = _admit_resting_target_order(store, "EARLY")

    assert (
        store.request_resting_order_cancel(
            order_id, reason="stale research quote: test", requested_at=row["created_at"]
        )
        is None
    )
    assert store.paper_order(order_id)["expires_at"] == row["expires_at"]


@pytest.mark.parametrize("lcb_offset", [0.0, 0.03])
def test_resting_quote_with_non_negative_current_edge_is_left_alone(
    tmp_path: Path, lcb_offset: float
) -> None:
    store = PaperStore(tmp_path / "fresh.db", research_clock=_fixed_research_clock)
    order_id, row, decision = _admit_resting_target_order(store, "FRESH")
    still_good = replace(decision, probability_lcb=float(row["cost_per_contract"]) + lcb_offset)

    cancelled = _research_trader(store).cancel_stale_research_resting_orders(
        "2026-07-19", [still_good]
    )

    assert cancelled == []
    assert store.paper_order(order_id)["status"] == "PAPER_LIMIT_RESTING"


def test_stale_guard_ignores_other_markets_sides_dates_and_missing_decisions(
    tmp_path: Path,
) -> None:
    store = PaperStore(tmp_path / "scope.db", research_clock=_fixed_research_clock)
    order_id, row, decision = _admit_resting_target_order(store, "SCOPE")
    stale_lcb = float(row["cost_per_contract"]) - 0.10
    trader = _research_trader(store)

    other_market = replace(decision, ticker=f"{decision.ticker}-OTHER", probability_lcb=stale_lcb)
    other_side = replace(decision, side="YES", probability_lcb=stale_lcb)
    assert trader.cancel_stale_research_resting_orders("2026-07-19", [other_market, other_side]) == []
    # Same market and side, but a different target date's scan.
    same_market_stale = replace(decision, probability_lcb=stale_lcb)
    assert trader.cancel_stale_research_resting_orders("2026-07-20", [same_market_stale]) == []
    # No fresh decision at all for the market this tick: missing information
    # is not evidence of staleness.
    assert trader.cancel_stale_research_resting_orders("2026-07-19", []) == []
    assert store.paper_order(order_id)["status"] == "PAPER_LIMIT_RESTING"


def test_stale_guard_requires_the_research_profile(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "profile.db")
    live = PaperTrader(store, strategy_config_for_profile("live"), risk_profile="live")
    with pytest.raises(ValueError, match="research profile"):
        live.cancel_stale_research_resting_orders("2026-07-19", [])


def _run_research_scan_context_with_mock_trader(
    *,
    place_paper: bool,
    place_research_target: bool | None,
    entry_allowed: bool = True,
) -> Mock:
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
    store.research_station_day.return_value = date(2026, 9, 11)
    store.research_account_state.return_value = {
        "available_cash": 900.0,
        "open_cost_basis": 0.0,
        "reservations": 0.0,
    }
    store.research_realized_pnl_for_day.return_value = 0.0
    trader = Mock()
    with patch.object(scan_module, "PaperTrader", return_value=trader):
        scan_module._execute_research_scan_context(
            context,
            target=date(2026, 9, 12),
            store=store,
            config=strategy_config_for_profile("research"),
            entry_allowed=entry_allowed,
            entry_block_reason=None if entry_allowed else "paper entry disabled: test",
            place_paper=place_paper,
            place_research_target=place_research_target,
            forecast_snapshot_id=1,
            market_snapshot_id=2,
        )
    return trader


@pytest.mark.parametrize(
    ("place_paper", "place_research_target", "entry_allowed", "expect_pull"),
    [
        # `portfolio-scan --risk-profile research` without --place-paper.
        (False, None, True, False),
        # The runner's shadow mode (PAPER_PLACE_RESEARCH_TARGET=0).
        (False, False, True, False),
        (True, False, True, False),
        # The runner placing the target sleeve.
        (False, True, True, True),
        (True, None, True, True),
        # A placing tick whose entry is blocked still pulls: it only reduces risk.
        (True, None, False, True),
    ],
)
def test_research_scan_pulls_stale_quotes_only_when_target_placement_was_requested(
    place_paper: bool,
    place_research_target: bool | None,
    entry_allowed: bool,
    expect_pull: bool,
) -> None:
    """Release review, MEDIUM: a dry-run scan wrote PAPER_EXPIRED rows."""

    trader = _run_research_scan_context_with_mock_trader(
        place_paper=place_paper,
        place_research_target=place_research_target,
        entry_allowed=entry_allowed,
    )

    assert trader.cancel_stale_research_resting_orders.called is expect_pull


def test_research_scan_cancels_stale_quotes_before_planning_and_admission() -> None:
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
    store.research_station_day.return_value = date(2026, 9, 11)
    store.research_account_state.return_value = {
        "available_cash": 900.0,
        "open_cost_basis": 0.0,
        "reservations": 0.0,
    }
    store.research_realized_pnl_for_day.return_value = 0.0
    trader = Mock()
    manager = Mock()
    manager.attach_mock(store, "store")
    manager.attach_mock(trader, "trader")
    with patch.object(scan_module, "PaperTrader", return_value=trader):
        scan_module._execute_research_scan_context(
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
    trader.cancel_stale_research_resting_orders.assert_called_once()
    (target_date, decisions), _ = trader.cancel_stale_research_resting_orders.call_args
    assert target_date == "2026-09-12"
    assert [d.ticker for d in decisions] == [context.decisions[0].ticker]
    names = [name for name, _, _ in manager.mock_calls]
    cancel_idx = names.index("trader.cancel_stale_research_resting_orders")
    assert cancel_idx < names.index("store.research_account_state")
    assert cancel_idx < names.index("trader.execute_research_plans")


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
