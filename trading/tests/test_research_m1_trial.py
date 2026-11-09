"""M1 market-recalibration paper trial: account, scan, placement, stops, evidence.

Covers the separate $2,000 ledger and its startup/deploy contract, the
research scan feeding the trial (first research scan of each UTC hour, shadow
mode, one entry per leg, filters, sizing), hold-to-settlement through the
monitor and the normal settle path, account isolation, every stop rule and its
durability, the daily reconciliation and the published book.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sfo_kalshi_quant import research_m1 as m1
from sfo_kalshi_quant import research_m1_store as engine
from sfo_kalshi_quant._cli import scan as scan_module
from sfo_kalshi_quant.account import (
    LIVE_STABILITY_ACCOUNT_ID,
    RESEARCH_ACCOUNT_ID,
    SHARED_ACCOUNT_ID,
    STRATEGY_BEHAVIOR_VERSION,
)
from sfo_kalshi_quant.cities import get_city
from sfo_kalshi_quant.cli import build_parser, main
from sfo_kalshi_quant.colors import Color
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.maker_fills import EXECUTION_MODEL_VERSION
from sfo_kalshi_quant.models import EventSnapshot, ForecastSnapshot, MarketBin, TradeDecision
from sfo_kalshi_quant.profile_identity import (
    execution_profile_key,
    published_profile_key,
    row_published_profile_key,
)
from sfo_kalshi_quant.replay import _readiness_root_profile_class
from sfo_kalshi_quant.research_policy import ALL_RESEARCH_POLICIES, TARGET_POLICY
from sfo_kalshi_quant.strategy_lab.profiles import _profile_label

FORECASTER = Path(__file__).resolve().parents[2] / "forecaster"
if str(FORECASTER) not in sys.path:
    sys.path.append(str(FORECASTER))

import city_truth  # noqa: E402  -- the forecaster's own settlement-truth writer

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = REPO_ROOT / "trading" / "deploy" / "aws" / "validate_account_cutover.py"

NOW = datetime(2026, 10, 20, 19, 4, tzinfo=UTC)  # 12:04 PDT at LAX: bucket L1b
TARGET = date(2026, 10, 21)
LAX = get_city("lax")
EVENT = "KXHIGHLAX-26OCT21"
CLOSE = "2026-10-22T07:59:00Z"
YES_TICKER = f"{EVENT}-B70.5"
MID_TICKER = f"{EVENT}-B72.5"
TAIL_TICKER = f"{EVENT}-T75"
YES_LEG = (TARGET.isoformat(), YES_TICKER, "YES")
TAIL_LEG = (TARGET.isoformat(), TAIL_TICKER, "NO")
M1 = m1.M1_ACCOUNT_ID


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _market(
    ticker: str,
    label: str,
    strike: str,
    floor: float | None,
    cap: float | None,
    *,
    yes_bid: float,
    yes_ask: float,
    yes_bid_size: float = 30.0,
    yes_ask_size: float = 40.0,
    no_bid_size: float = 40.0,
    no_ask_size: float = 30.0,
    status: str = "active",
    event: str = EVENT,
    close: str = CLOSE,
) -> MarketBin:
    return MarketBin(
        ticker=ticker,
        event_ticker=event,
        title="Highest temperature in Los Angeles?",
        yes_sub_title=label,
        strike_type=strike,
        floor_strike=floor,
        cap_strike=cap,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=round(1.0 - yes_ask, 2),
        no_ask=round(1.0 - yes_bid, 2),
        yes_bid_size=yes_bid_size,
        yes_ask_size=yes_ask_size,
        status=status,
        raw={
            "close_time": close,
            "no_bid_size_fp": no_bid_size,
            "no_ask_size_fp": no_ask_size,
        },
    )


def _decision(
    market: MarketBin,
    side: str,
    *,
    market_probability: float,
    model_probability: float,
) -> TradeDecision:
    if side == "YES":
        bid, ask = market.yes_bid, market.yes_ask
        bid_size, ask_size = market.yes_bid_size, market.yes_ask_size
    else:
        bid, ask = market.no_bid, market.no_ask
        bid_size, ask_size = market.no_bid_size, market.no_ask_size
    fee = m1.gate_fee(ask)
    return TradeDecision(
        ticker=market.ticker,
        label=market.yes_sub_title,
        action=f"BUY_{side}",
        approved=True,
        probability=model_probability,
        probability_lcb=max(0.0, model_probability - 0.1),
        yes_bid=market.yes_bid,
        yes_ask=market.yes_ask,
        spread=round(ask - bid, 2),
        fee_per_contract=fee,
        cost_per_contract=ask + fee,
        edge=model_probability - ask - fee,
        edge_lcb=model_probability - 0.1 - ask - fee,
        kelly_fraction=0.0,
        recommended_contracts=25.0,
        expected_profit=0.0,
        reasons=[],
        yes_ask_size=market.yes_ask_size,
        side=side,
        entry_bid=bid,
        entry_ask=ask,
        entry_bid_size=bid_size,
        entry_ask_size=ask_size,
        strike_type=market.strike_type,
        floor_strike=market.floor_strike,
        cap_strike=market.cap_strike,
        model_probability=model_probability,
        market_probability=market_probability,
        trade_quality_score=50.0,
    )


def _ladder(
    *,
    target: date = TARGET,
    yes_bid: float = 0.46,
    yes_ask: float = 0.48,
    yes_market_probability: float = 0.50,
    yes_model_probability: float = 0.55,
) -> tuple[list[MarketBin], list[TradeDecision]]:
    """Two signals: B70.5 YES (model agrees) and T75 NO (model disagrees)."""

    token = target.strftime("%y") + target.strftime("%b").upper() + target.strftime("%d")
    event = f"KXHIGHLAX-{token}"
    close = (datetime(target.year, target.month, target.day, 7, 59, tzinfo=UTC) + timedelta(days=1)).isoformat()
    yes_market = _market(
        f"{event}-B70.5", "70° to 71°", "between", 70.0, 71.0,
        yes_bid=yes_bid, yes_ask=yes_ask, event=event, close=close,
    )
    mid_market = _market(
        f"{event}-B72.5", "72° to 73°", "between", 72.0, 73.0,
        yes_bid=0.18, yes_ask=0.20, no_bid_size=20.0, no_ask_size=20.0,
        event=event, close=close,
    )
    tail_market = _market(
        f"{event}-T75", "75° or above", "greater", 74.0, None,
        yes_bid=0.08, yes_ask=0.10, no_bid_size=15.0, no_ask_size=6.0,
        event=event, close=close,
    )
    markets = [yes_market, mid_market, tail_market]
    decisions = [
        _decision(yes_market, "YES", market_probability=yes_market_probability,
                  model_probability=yes_model_probability),
        _decision(yes_market, "NO", market_probability=1 - yes_market_probability,
                  model_probability=1 - yes_model_probability),
        _decision(mid_market, "YES", market_probability=0.20, model_probability=0.22),
        _decision(mid_market, "NO", market_probability=0.80, model_probability=0.78),
        _decision(tail_market, "YES", market_probability=0.06, model_probability=0.10),
        _decision(tail_market, "NO", market_probability=0.94, model_probability=0.90),
    ]
    return markets, decisions


def _forecast(**overrides) -> ForecastSnapshot:
    values = dict(
        target_date=TARGET,
        predicted_high_f=70.0,
        station_id="KLAX",
        fetched_at=(NOW - timedelta(hours=2)).isoformat(),
        source_count=3,
        source_spread_override_f=1.5,
        method="emos",
    )
    values.update(overrides)
    return ForecastSnapshot(**values)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _store(tmp_path: Path, now: datetime = NOW) -> tuple[PaperStore, _Clock]:
    clock = _Clock(now)
    return PaperStore(tmp_path / "paper.db", research_clock=clock), clock


def _scan(
    store: PaperStore,
    *,
    place: bool = True,
    now: datetime | None = None,
    target: date = TARGET,
    ladder: tuple[list[MarketBin], list[TradeDecision]] | None = None,
    forecast: ForecastSnapshot | None = None,
    entry_gate_reason: str | None = None,
) -> engine.M1ScanResult:
    markets, decisions = ladder or _ladder(target=target)
    return engine.run_m1_scan(
        store,
        target_date=target,
        city=LAX,
        decisions=decisions,
        markets=markets,
        forecast=forecast or _forecast(target_date=target),
        entry_gate_reason=entry_gate_reason,
        place=place,
        now=now,
    )


def _rows(store: PaperStore, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with store.connect() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()


def _m1_orders(store: PaperStore) -> list[sqlite3.Row]:
    return _rows(store, "SELECT * FROM paper_orders WHERE account_id=? ORDER BY id", (M1,))


def _statuses(store: PaperStore) -> dict[str, str]:
    return {
        str(row["account_id"]): str(row["status"])
        for row in _rows(store, "SELECT account_id, status FROM paper_accounts")
    }


def _insert_m1_order(
    store: PaperStore,
    *,
    target_date: str,
    ticker: str,
    side: str = "YES",
    created_at: str,
    contracts: float = 10.0,
    entry_price: float = 0.98,
    entry_bid: float = 0.96,
    status: str = "PAPER_SETTLED",
    realized_pnl: float | None = None,
) -> int:
    """Write a complete M1 lot (identity, fill debit and settlement proceeds)."""

    cost = entry_price
    stake = contracts * cost
    settled = status == "PAPER_SETTLED"
    with store.connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO paper_orders (
                created_at, target_date, market_ticker, label, action, risk_profile,
                side, contracts, yes_ask, entry_price, entry_bid, fee_per_contract,
                cost_per_contract, probability, probability_lcb, edge, edge_lcb,
                trade_quality_score, expected_profit, status, reasons_json,
                settled_at, realized_pnl, account_id, research_sleeve,
                research_policy_version, policy_fingerprint, strategy_fingerprint,
                execution_model_version, fill_model, filled_contracts
            ) VALUES (?, ?, ?, 'synthetic', ?, 'research', ?, ?, ?, ?, ?, 0, ?, 0.9,
                      0.9, 0.0, 0.0, 0, 0.0, ?, '[]', ?, ?, ?, ?, ?, ?, ?, ?,
                      'immediate_visible_quote', ?)
            """,
            (
                created_at, target_date, ticker, f"BUY_{side}", side, contracts,
                entry_price, entry_price, entry_bid, cost, status,
                created_at if settled else None,
                realized_pnl if settled else None,
                M1, m1.M1_POLICY.sleeve, m1.M1_POLICY.policy_version,
                m1.M1_POLICY.policy_fingerprint, m1.M1_POLICY.policy_fingerprint,
                EXECUTION_MODEL_VERSION, contracts,
            ),
        )
        order_id = int(cursor.lastrowid)
        conn.execute(
            "INSERT INTO paper_account_ledger (created_at, account_id, order_id, "
            "event_type, amount, idempotency_key) VALUES (?, ?, ?, 'ENTRY_FILL', ?, ?)",
            (created_at, M1, order_id, -stake, f"order:{order_id}:entry-fill"),
        )
        if settled:
            conn.execute(
                "INSERT INTO paper_account_ledger (created_at, account_id, order_id, "
                "event_type, amount, idempotency_key) "
                "VALUES (?, ?, ?, 'SETTLEMENT_PROCEEDS', ?, ?)",
                (
                    created_at, M1, order_id, stake + float(realized_pnl or 0.0),
                    f"order:{order_id}:settlement-proceeds",
                ),
            )
    return order_id


def _insert_settled_book(
    store: PaperStore,
    count: int,
    *,
    pnl_each: float,
    start: date = date(2026, 8, 1),
    per_day: int = 10,
    offset: int = 0,
) -> None:
    for index in range(offset, offset + count):
        target = start + timedelta(days=index // per_day)
        _insert_m1_order(
            store,
            target_date=target.isoformat(),
            ticker=f"KXHIGHLAX-SYN{index:05d}-B70.5",
            created_at=datetime(target.year, target.month, target.day, tzinfo=UTC).isoformat(),
            contracts=10.0,
            entry_price=0.98,
            realized_pnl=pnl_each,
        )


def _halt_rules(store: PaperStore) -> set[str]:
    with store.connect() as conn:
        return {halt["rule"] for halt in engine.m1_halts(conn)}


# --------------------------------------------------------------------------
# Account, startup and the deploy contract
# --------------------------------------------------------------------------


def test_fresh_store_bootstraps_the_separate_two_thousand_dollar_ledger(tmp_path) -> None:
    store, _ = _store(tmp_path)
    PaperStore(tmp_path / "paper.db")
    PaperStore(tmp_path / "paper.db")

    state = store._account_state(M1, persist_high_water=False)
    assert state["status"] == "ACTIVE"
    assert state["initial_capital"] == state["opening_cash"] == m1.M1_POLICY.reference_equity
    assert state["available_cash"] == state["realized_equity"] == m1.M1_POLICY.reference_equity
    ledger = _rows(store, "SELECT * FROM paper_account_ledger WHERE account_id=?", (M1,))
    assert [(row["event_type"], row["amount"], row["idempotency_key"]) for row in ledger] == [
        ("OPENING_CASH", m1.M1_POLICY.reference_equity, f"{M1}:opening")
    ]
    details = json.loads(ledger[0]["details_json"])
    assert details["policy_fingerprint"] == m1.M1_POLICY.policy_fingerprint
    assert details["paper_only"] is True
    assert store.account_order_ledger_reconciliation(M1)["status"] == "reconciled"
    statuses = _statuses(store)
    assert statuses[M1] == "ACTIVE"
    assert statuses[LIVE_STABILITY_ACCOUNT_ID] == "ACTIVE"
    assert statuses[TARGET_POLICY.account_id] == "ACTIVE"
    for policy in ALL_RESEARCH_POLICIES:
        if policy is not TARGET_POLICY:
            assert statuses[policy.account_id] == "ARCHIVED"
    assert statuses[SHARED_ACCOUNT_ID] == statuses[RESEARCH_ACCOUNT_ID] == "ARCHIVED"


def test_startup_keeps_m1_active_without_touching_any_other_account(tmp_path) -> None:
    store, _ = _store(tmp_path)
    before = _statuses(store)
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO paper_accounts (account_id, created_at, initial_capital, "
            "opening_cash, high_water_equity, status) VALUES "
            "('paper-custom-legacy', ?, 1000, 1000, 1000, 'ACTIVE')",
            (NOW.isoformat(),),
        )
    for _ in range(3):
        PaperStore(tmp_path / "paper.db")
    after = _statuses(store)
    assert after.pop("paper-custom-legacy") == "ARCHIVED"
    assert after == before
    assert after[M1] == "ACTIVE"

    # An M1 ledger something else archived stays archived (fail closed), and
    # no other account is un-archived either.
    with store.connect() as conn:
        conn.execute("UPDATE paper_accounts SET status='ARCHIVED' WHERE account_id=?", (M1,))
    PaperStore(tmp_path / "paper.db")
    final = _statuses(store)
    assert final[M1] == "ARCHIVED"
    assert {k: v for k, v in final.items() if k not in {M1, "paper-custom-legacy"}} == {
        k: v for k, v in before.items() if k != M1
    }
    result = _scan(store)
    assert result.order_ids == []
    assert result.blocked == [
        (YES_TICKER, "M1 paper account is not active"),
        (TAIL_TICKER, "M1 paper account is not active"),
    ]


def _run_validator(db_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), "--db", str(db_path)],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "trading")},
        text=True,
        capture_output=True,
        check=False,
    )


def test_deploy_validator_requires_live_v7_and_an_active_m1_at_their_capital(tmp_path) -> None:
    db_path = tmp_path / "paper.db"
    store = PaperStore(db_path)
    healthy = _run_validator(db_path)
    assert healthy.returncode == 0, healthy.stderr
    assert "Live Stability, Research V7, M1 trial" in healthy.stdout

    with store.connect() as conn:
        conn.execute("UPDATE paper_accounts SET opening_cash=1000 WHERE account_id=?", (M1,))
    tampered = _run_validator(db_path)
    assert tampered.returncode != 0
    assert f"active paper account capital is invalid: {M1}" in tampered.stderr

    with store.connect() as conn:
        conn.execute("UPDATE paper_accounts SET opening_cash=10000 WHERE account_id=?", (M1,))
        conn.execute("UPDATE paper_accounts SET status='ARCHIVED' WHERE account_id=?", (M1,))
    # A rollback past M1 archives it; the roll-forward must not fail the deploy
    # (and leave the host quiesced) over it.
    archived = _run_validator(db_path)
    assert archived.returncode == 0, archived.stderr
    assert f"WARNING: M1 trial ledger {M1} is not active" in archived.stderr

    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_accounts SET status='ARCHIVED' WHERE account_id=?",
            (LIVE_STABILITY_ACCOUNT_ID,),
        )
    no_live = _run_validator(db_path)
    assert no_live.returncode != 0
    assert "active paper account set does not match the restart policy" in no_live.stderr


def test_deploy_validator_fails_when_the_m1_ledger_does_not_reconcile(tmp_path) -> None:
    db_path = tmp_path / "paper.db"
    store = PaperStore(db_path, research_clock=_Clock(NOW))
    result = _scan(store)
    assert len(result.order_ids) == 2
    assert _run_validator(db_path).returncode == 0
    with store.connect() as conn:
        conn.execute(
            "DELETE FROM paper_account_ledger WHERE order_id=? AND event_type='ENTRY_FILL'",
            (result.order_ids[0],),
        )
    broken = _run_validator(db_path)
    assert broken.returncode != 0
    assert f"active paper account order ledger does not reconcile: {M1}" in broken.stderr


# --------------------------------------------------------------------------
# Scan -> placement
# --------------------------------------------------------------------------


def test_placement_books_taker_fills_at_the_displayed_ask_with_per_order_fees(tmp_path) -> None:
    store, _ = _store(tmp_path)
    result = _scan(store)

    assert (result.legs, result.sampled, result.candidates) == (6, 6, 2)
    assert len(result.order_ids) == 2 and result.blocked == []
    yes, tail = _m1_orders(store)
    assert (yes["market_ticker"], yes["side"]) == (YES_TICKER, "YES")
    assert yes["contracts"] == 20.0  # $10 incl. the 35c order fee at a 48c ask
    assert yes["entry_price"] == yes["yes_ask"] == 0.48
    assert yes["fee_per_contract"] == pytest.approx(0.35 / 20)
    assert yes["contracts"] * yes["cost_per_contract"] == pytest.approx(9.95)
    assert (tail["market_ticker"], tail["side"]) == (TAIL_TICKER, "NO")
    assert tail["contracts"] == 6.0  # clamped to the 6 displayed NO contracts
    assert tail["entry_price"] == 0.92
    assert tail["contracts"] * tail["cost_per_contract"] == pytest.approx(5.56)
    for order in (yes, tail):
        assert order["status"] == "PAPER_FILLED"
        assert order["fill_model"] == "immediate_visible_quote"
        assert order["entry_mode"] == "market"
        assert order["limit_price"] is None and order["expires_at"] is None
        assert order["reserved_cost"] == 0
        assert order["risk_profile"] == "research"
        assert order["account_id"] == M1
        assert order["research_sleeve"] == "m1"
        assert order["research_policy_version"] == m1.M1_POLICY.policy_version
        assert order["policy_fingerprint"] == m1.M1_POLICY.policy_fingerprint
        assert order["strategy_fingerprint"] == m1.M1_POLICY.policy_fingerprint
        assert order["execution_model_version"] == EXECUTION_MODEL_VERSION
        assert order["lead_bucket"] == "day-ahead"
        assert order["created_at"] == NOW.isoformat()
        assert row_published_profile_key(order) == "research-m1"
    diagnostics = json.loads(yes["diagnostics_json"])
    assert diagnostics["bucket"] == "L1b"
    assert diagnostics["q_yes"] == pytest.approx(0.5)
    assert diagnostics["agree"] is True
    assert diagnostics["coefficient_set_id"] == "m1-deploy-coefs-2026-10-28"
    assert json.loads(tail["diagnostics_json"])["agree"] is False

    ledger = _rows(
        store,
        "SELECT order_id, event_type, amount FROM paper_account_ledger "
        "WHERE account_id=? AND order_id IS NOT NULL ORDER BY id",
        (M1,),
    )
    assert [(row["event_type"], round(row["amount"], 2)) for row in ledger] == [
        ("ENTRY_FILL", -9.95),
        ("ENTRY_FILL", -5.56),
    ]
    state = store._account_state(M1, persist_high_water=False)
    assert state["available_cash"] == pytest.approx(m1.M1_POLICY.reference_equity - 15.51)
    assert state["open_cost_basis"] == pytest.approx(15.51)
    assert state["realized_equity"] == pytest.approx(m1.M1_POLICY.reference_equity)
    assert store.account_order_ledger_reconciliation(M1)["status"] == "reconciled"

    evaluations = _rows(store, "SELECT * FROM m1_evaluations ORDER BY id")
    assert len(evaluations) == 6
    by_leg = {(row["market_ticker"], row["side"]): row for row in evaluations}
    entered = by_leg[(YES_TICKER, "YES")]
    assert (entered["decision"], entered["order_id"]) == ("ENTERED", yes["id"])
    assert entered["agree"] == 1 and entered["bucket"] == "L1b"
    assert entered["utc_hour"] == "2026-10-20T19"
    assert entered["coefficient_set_id"] == "m1-deploy-coefs-2026-10-28"
    assert by_leg[(TAIL_TICKER, "NO")]["agree"] == 0
    assert sorted(row["decision"] for row in evaluations) == [
        "ENTERED", "ENTERED", "NO_SIGNAL", "NO_SIGNAL", "NO_SIGNAL", "NO_SIGNAL",
    ]
    shadow = _rows(store, "SELECT arm, market_ticker, side FROM m1_shadow_entries ORDER BY id")
    assert sorted((row["arm"], row["market_ticker"]) for row in shadow) == [
        ("m1_5min", YES_TICKER),
        ("m1_5min", TAIL_TICKER),
        ("mv_hourly", YES_TICKER),  # the joint crossing needs p_model >= cost
    ]


def test_shadow_mode_records_every_decision_and_places_nothing(tmp_path) -> None:
    store, _ = _store(tmp_path)
    result = _scan(store, place=False)

    assert result.order_ids == [] and result.candidates == 0
    assert _m1_orders(store) == []
    evaluations = _rows(store, "SELECT * FROM m1_evaluations ORDER BY id")
    shadow_rows = [row for row in evaluations if row["decision"] == "SHADOW"]
    assert {(row["market_ticker"], row["side"]) for row in shadow_rows} == {
        (YES_TICKER, "YES"),
        (TAIL_TICKER, "NO"),
    }
    assert {row["reason"] for row in shadow_rows} == {"M1 placement disabled (shadow mode)"}
    assert {row["placement_enabled"] for row in evaluations} == {0}
    ledger = _rows(store, "SELECT event_type FROM paper_account_ledger WHERE account_id=?", (M1,))
    assert [row["event_type"] for row in ledger] == ["OPENING_CASH"]
    assert len(_rows(store, "SELECT * FROM m1_shadow_entries")) == 3


def test_only_the_first_research_scan_of_each_utc_hour_is_sampled(tmp_path) -> None:
    store, _ = _store(tmp_path)
    assert len(_scan(store, now=NOW).order_ids) == 2

    again = _scan(store, now=NOW + timedelta(minutes=5))
    assert (again.sampled, again.order_ids) == (0, [])
    assert len(_rows(store, "SELECT id FROM m1_evaluations")) == 6

    next_hour = _scan(store, now=datetime(2026, 10, 20, 20, 3, tzinfo=UTC))
    assert (next_hour.sampled, next_hour.order_ids) == (6, [])
    decisions = {
        (row["market_ticker"], row["side"]): row["decision"]
        for row in _rows(
            store, "SELECT * FROM m1_evaluations WHERE utc_hour='2026-10-20T20'"
        )
    }
    assert decisions[(YES_TICKER, "YES")] == "ALREADY_ENTERED"
    assert decisions[(TAIL_TICKER, "NO")] == "ALREADY_ENTERED"

    # The sample is the first research scan in the hour, wherever its minute
    # falls: a scan that starts seven minutes late is still the sample.
    late = _scan(store, now=datetime(2026, 10, 20, 21, 7, tzinfo=UTC))
    assert late.sampled == 6
    assert len(_m1_orders(store)) == 2


def test_a_crossing_between_samples_waits_for_the_next_hour(tmp_path) -> None:
    store, _ = _store(tmp_path)
    no_signal = _ladder(yes_bid=0.50, yes_ask=0.52)  # YES edge below 2 cents
    first = _scan(store, now=NOW, ladder=no_signal)
    assert [order["market_ticker"] for order in _m1_orders(store)] == [TAIL_TICKER]
    assert first.sampled == 6

    _scan(store, now=NOW + timedelta(minutes=5))  # crosses, but not a sample
    assert [order["market_ticker"] for order in _m1_orders(store)] == [TAIL_TICKER]
    five_minute = _rows(
        store,
        "SELECT created_at FROM m1_shadow_entries WHERE arm='m1_5min' AND market_ticker=?",
        (YES_TICKER,),
    )
    assert [row["created_at"] for row in five_minute] == [
        (NOW + timedelta(minutes=5)).isoformat()
    ]

    _scan(store, now=datetime(2026, 10, 20, 20, 2, tzinfo=UTC))
    yes_orders = [o for o in _m1_orders(store) if o["market_ticker"] == YES_TICKER]
    assert [o["created_at"] for o in yes_orders] == [
        datetime(2026, 10, 20, 20, 2, tzinfo=UTC).isoformat()
    ]


def test_one_entry_per_leg_is_enforced_by_the_database_forever(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _scan(store)
    store.settle_paper_orders(TARGET.isoformat(), 70.0, series_ticker="KXHIGHLAX")
    assert {o["status"] for o in _m1_orders(store)} == {"PAPER_SETTLED"}
    with pytest.raises(sqlite3.IntegrityError):
        _insert_m1_order(
            store,
            target_date=TARGET.isoformat(),
            ticker=YES_TICKER,
            created_at=NOW.isoformat(),
            status="PAPER_FILLED",
        )
    # The same leg in another account is unaffected.
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO paper_orders (created_at, target_date, market_ticker, label, "
            "action, risk_profile, side, contracts, yes_ask, fee_per_contract, "
            "cost_per_contract, probability, probability_lcb, edge, edge_lcb, "
            "expected_profit, status, reasons_json, account_id, research_sleeve, "
            "research_policy_version, policy_fingerprint) VALUES (?, ?, ?, 'x', "
            "'BUY_YES', 'research', 'YES', 1, 0.48, 0.02, 0.5, 0.6, 0.5, 0.1, 0.0, "
            "0.1, 'PAPER_FILLED', '[]', ?, ?, ?, ?)",
            (
                NOW.isoformat(), TARGET.isoformat(), YES_TICKER,
                TARGET_POLICY.account_id, TARGET_POLICY.sleeve.value,
                TARGET_POLICY.policy_version, TARGET_POLICY.policy_fingerprint,
            ),
        )


def test_listing_hour_and_wide_spreads_are_filtered_and_lead_must_be_one(tmp_path) -> None:
    store, _ = _store(tmp_path)
    # 14:10 UTC on Oct 20 is 10:10 EDT on D-1 (07:10 PDT at LAX, still lead 1).
    listing = _scan(store, now=datetime(2026, 10, 20, 14, 10, tzinfo=UTC))
    assert listing.order_ids == []
    reasons = {
        row["reason"]
        for row in _rows(store, "SELECT reason FROM m1_evaluations WHERE utc_hour='2026-10-20T14'")
    }
    assert reasons == {"listing hour (D-1 10:00-10:59 America/New_York)"}

    wide = _scan(
        store,
        now=datetime(2026, 10, 20, 16, 1, tzinfo=UTC),
        ladder=_ladder(yes_bid=0.37, yes_ask=0.48),
    )
    assert [order["market_ticker"] for order in _m1_orders(store)] == [TAIL_TICKER]
    assert wide.order_ids and len(wide.order_ids) == 1
    yes_row = _rows(
        store,
        "SELECT decision, reason FROM m1_evaluations WHERE utc_hour='2026-10-20T16' "
        "AND market_ticker=? AND side='YES'",
        (YES_TICKER,),
    )[0]
    assert (yes_row["decision"], yes_row["reason"]) == ("FILTERED", "spread 11c above 10c")

    two_days = _scan(store, now=NOW, target=TARGET + timedelta(days=1))
    assert two_days.skipped_reason == "target is not the next civil day at the station"
    assert _rows(
        store, "SELECT id FROM m1_evaluations WHERE target_date=?",
        ((TARGET + timedelta(days=1)).isoformat(),),
    ) == []


def test_sizing_clamps_to_the_25_contract_cap(tmp_path) -> None:
    store, _ = _store(tmp_path)
    cheap = _ladder(yes_bid=0.29, yes_ask=0.30, yes_market_probability=0.36,
                    yes_model_probability=0.38)
    _scan(store, ladder=cheap)
    yes = [o for o in _m1_orders(store) if o["market_ticker"] == YES_TICKER][0]
    assert yes["contracts"] == 25.0  # 31 would fit $10; the per-market cap binds
    assert yes["contracts"] * yes["cost_per_contract"] == pytest.approx(7.5 + 0.37)


def test_existing_risk_limits_block_m1_entries(tmp_path) -> None:
    store, _ = _store(tmp_path)
    gapped = _ladder(yes_model_probability=0.80)  # gap 0.30 > 0.25
    _scan(store, ladder=gapped, forecast=_forecast(source_spread_override_f=7.4))
    assert _m1_orders(store) == []
    reasons = {
        row["reason"]
        for row in _rows(store, "SELECT reason FROM m1_evaluations WHERE decision='RISK_BLOCKED'")
    }
    assert reasons == {"forecast source spread 7.4F exceeds 7.3F", "model/market gap 0.300 exceeds 0.25"}

    stale = _scan(
        store,
        now=NOW + timedelta(hours=1),
        forecast=_forecast(fetched_at=(NOW - timedelta(hours=40)).isoformat()),
    )
    assert stale.order_ids == []
    single = _scan(
        store,
        now=NOW + timedelta(hours=2),
        entry_gate_reason="paper entry disabled: single-source forecast",
    )
    assert single.order_ids == []
    assert _m1_orders(store) == []


def test_exposure_caps_apply_to_this_accounts_own_equity(tmp_path) -> None:
    store, _ = _store(tmp_path)
    # $85 already open on the target date (another city): the $9.95 YES entry
    # still fits the $100 (5% of $2,000) target-date cap, the $5.56 tail
    # entry after it does not.
    _insert_m1_order(
        store,
        target_date=TARGET.isoformat(),
        ticker="KXHIGHNY-26OCT21-B60.5",
        created_at=(NOW - timedelta(hours=3)).isoformat(),
        contracts=100.0,
        entry_price=0.85,
        status="PAPER_FILLED",
    )
    result = _scan(store)
    assert result.blocked == [
        (TAIL_TICKER, "target-date exposure $100.51 would exceed $100.00")
    ]
    assert [o["market_ticker"] for o in _m1_orders(store)][1:] == [YES_TICKER]
    tail = _rows(
        store,
        "SELECT decision FROM m1_evaluations WHERE market_ticker=? AND side='NO'",
        (TAIL_TICKER,),
    )
    assert [row["decision"] for row in tail] == ["ACCOUNT_BLOCKED"]


# --------------------------------------------------------------------------
# Hold to settlement
# --------------------------------------------------------------------------


def test_the_monitor_never_exits_an_m1_position(tmp_path) -> None:
    store, _ = _store(tmp_path)
    order_ids = _scan(store).order_ids
    requested: list[str] = []

    class CrashedMarketClient:
        def get_market(self, ticker):
            requested.append(ticker)
            return MarketBin(
                ticker=ticker, event_ticker=EVENT, title="", yes_sub_title="",
                strike_type="between", floor_strike=70.0, cap_strike=71.0,
                yes_bid=0.01, yes_ask=0.02, no_bid=0.98, no_ask=0.99,
                yes_bid_size=100.0, yes_ask_size=100.0, status="active",
            )

    out = io.StringIO()
    with patch("sfo_kalshi_quant.cli.KalshiPublicClient", CrashedMarketClient), redirect_stdout(out):
        assert main(["--db-path", str(tmp_path / "paper.db"), "--no-color", "paper-monitor"]) == 0

    assert requested == []  # not even a quote fetch reaches an M1 lot
    for order in _m1_orders(store):
        assert order["status"] == "PAPER_FILLED"
        assert order["closed_at"] is None and order["exit_price"] is None
    snapshots = _rows(
        store,
        "SELECT order_id, action, account_id, research_sleeve, policy_fingerprint "
        "FROM paper_monitor_snapshots ORDER BY id",
    )
    assert {row["order_id"] for row in snapshots} == set(order_ids)
    assert {row["action"] for row in snapshots} == {"HOLD_TO_SETTLEMENT"}
    assert {row["research_sleeve"] for row in snapshots} == {"m1"}
    assert "held to settlement" in out.getvalue()


def test_the_same_day_heartbeat_never_reads_for_an_m1_lot(tmp_path, monkeypatch) -> None:
    store, _ = _store(tmp_path)
    assert _scan(store).order_ids
    passed: list = []
    monkeypatch.setenv("PAPER_SAME_DAY_MODEL_HEARTBEAT_ENABLED", "true")
    monkeypatch.setattr(
        "sfo_kalshi_quant.cli._refresh_same_day_model_reads",
        lambda store, rows, **kwargs: passed.extend(rows),
    )
    with patch("sfo_kalshi_quant.cli.KalshiPublicClient"), redirect_stdout(io.StringIO()):
        assert main(["--db-path", str(tmp_path / "paper.db"), "--no-color", "paper-monitor"]) == 0
    assert passed == []  # an M1 lot never starts a research-profile model read


def test_settlement_closes_m1_positions_through_the_account_scoped_settle(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _scan(store)
    settled = store.settle_paper_orders(TARGET.isoformat(), 70.0, series_ticker="KXHIGHLAX")
    assert settled == 2
    yes, tail = _m1_orders(store)
    assert yes["realized_pnl"] == pytest.approx(20 - 9.95)  # 70 is inside 70-71
    assert tail["realized_pnl"] == pytest.approx(6 - 5.56)  # 70 is not above 74
    state = store._account_state(M1, persist_high_water=False)
    assert state["open_cost_basis"] == 0.0
    assert state["realized_equity"] == pytest.approx(m1.M1_POLICY.reference_equity + 10.05 + 0.44)
    assert store.account_order_ledger_reconciliation(M1)["status"] == "reconciled"


# --------------------------------------------------------------------------
# Isolation, identity and real-money impossibility
# --------------------------------------------------------------------------


def test_no_cash_or_pnl_crosses_into_another_account(tmp_path) -> None:
    store, _ = _store(tmp_path)
    before = {
        account: store._account_state(account, persist_high_water=False)
        for account in (LIVE_STABILITY_ACCOUNT_ID, TARGET_POLICY.account_id)
    }
    _scan(store)
    store.settle_paper_orders(TARGET.isoformat(), 76.0, series_ticker="KXHIGHLAX")
    m1_state = store._account_state(M1, persist_high_water=False)
    assert m1_state["realized_equity"] == pytest.approx(m1.M1_POLICY.reference_equity - 15.51)
    for account, state in before.items():
        assert store._account_state(account, persist_high_water=False) == state
    other_ledger = _rows(
        store,
        "SELECT l.account_id FROM paper_account_ledger l JOIN paper_orders o "
        "ON o.id = l.order_id WHERE o.account_id=?",
        (M1,),
    )
    assert {row["account_id"] for row in other_ledger} == {M1}
    # Research V7's daily loss and capacity never see M1's losses.
    objective_day = store.research_objective_day()
    assert store.research_realized_pnl_for_day(
        account_id=TARGET_POLICY.account_id, objective_day=objective_day
    ) == 0.0
    assert store.research_open_risk(account_id=TARGET_POLICY.account_id) == 0.0


def test_identity_and_readiness_treat_m1_as_its_own_research_book() -> None:
    identity = dict(
        account_id=M1,
        research_sleeve="m1",
        research_policy_version=m1.M1_POLICY.policy_version,
        policy_fingerprint=m1.M1_POLICY.policy_fingerprint,
    )
    assert published_profile_key("research", **identity) == "research-m1"
    assert execution_profile_key("research-m1") == "research"
    crossed = {**identity, "policy_fingerprint": TARGET_POLICY.policy_fingerprint}
    assert published_profile_key("research", **crossed) == "unknown"
    partial = {**identity, "research_sleeve": None}
    assert published_profile_key("research", **partial) == "unknown"
    assert _readiness_root_profile_class({"risk_profile": "research", **identity}) == "research"
    assert _readiness_root_profile_class({"risk_profile": "research", **crossed}) == "invalid"
    assert published_profile_key(
        "research",
        account_id=TARGET_POLICY.account_id,
        research_sleeve=TARGET_POLICY.sleeve.value,
        research_policy_version=TARGET_POLICY.policy_version,
        policy_fingerprint=TARGET_POLICY.policy_fingerprint,
    ) == "research-target"
    assert _profile_label("research-m1") == "M1 market-recalibration trial (paper only)"


def test_m1_identity_is_pinned_to_this_release_and_independent_of_its_versions(
    tmp_path, monkeypatch
) -> None:
    """The M1 fingerprint is computed by this release and never rotates with it.

    This release stamps orders with exec-v5 and runs strategy behavior-v6
    (the Apple forecast nudge), which moved the live and research strategy
    fingerprints. M1's policy
    fingerprint hashes only its pre-registered constants, so it is the same
    value for the new capital decision, and a later release-wide version bump
    cannot orphan M1's open lots from their published book.
    """

    assert EXECUTION_MODEL_VERSION == "exec-v5-2026-10-05"
    assert STRATEGY_BEHAVIOR_VERSION == "behavior-v6-apple-forecast-2026-11-09"
    assert m1.M1_POLICY.policy_fingerprint == "f0258953ff7eb55a1e9318b4"
    import sfo_kalshi_quant.account as account_module
    import sfo_kalshi_quant.maker_fills as maker_fills_module

    monkeypatch.setattr(account_module, "STRATEGY_BEHAVIOR_VERSION", "behavior-next")
    monkeypatch.setattr(maker_fills_module, "EXECUTION_MODEL_VERSION", "exec-next")
    assert m1.M1_POLICY.policy_fingerprint == "f0258953ff7eb55a1e9318b4"
    monkeypatch.undo()

    store, _ = _store(tmp_path)
    _scan(store)
    PaperStore(tmp_path / "paper.db")  # startup's exec cutover leaves filled lots alone
    orders = _m1_orders(store)
    assert {o["execution_model_version"] for o in orders} == {"exec-v5-2026-10-05"}
    assert {o["status"] for o in orders} == {"PAPER_FILLED"}
    version = _rows(
        store, "SELECT config_json FROM strategy_versions WHERE fingerprint=?",
        (m1.M1_POLICY.policy_fingerprint,),
    )
    assert json.loads(version[0]["config_json"]) == m1.M1_POLICY.identity_payload()


# --------------------------------------------------------------------------
# Stop rules
# --------------------------------------------------------------------------


def test_loss_stop_halts_new_entries_durably_and_never_closes_positions(tmp_path) -> None:
    store, clock = _store(tmp_path)
    _scan(store)  # two open positions
    _insert_settled_book(store, 31, pnl_each=-10.0)  # -$310 realized
    status = engine.evaluate_m1_stop_rules(store, now=NOW)
    assert status["new_halts"] == ["loss_stop"]
    assert status["realized_pnl"] == pytest.approx(-310.0)
    halt = _rows(
        store,
        "SELECT event_type, amount, idempotency_key, details_json FROM paper_account_ledger "
        "WHERE account_id=? AND event_type='M1_HALT'",
        (M1,),
    )
    assert [(row["event_type"], row["amount"], row["idempotency_key"]) for row in halt] == [
        ("M1_HALT", 0.0, f"{M1}:halt:loss_stop")
    ]
    assert json.loads(halt[0]["details_json"])["automatic_resumption"] is False
    assert {o["status"] for o in _m1_orders(store) if o["target_date"] == TARGET.isoformat()} == {
        "PAPER_FILLED"
    }

    # A fresh process, a new hour and a recovering P&L still cannot resume.
    reopened = PaperStore(tmp_path / "paper.db", research_clock=clock)
    _insert_settled_book(reopened, 5, pnl_each=100.0, offset=31)
    assert engine.evaluate_m1_stop_rules(reopened, now=NOW)["new_halts"] == []
    other_day = _scan(reopened, now=datetime(2026, 10, 21, 19, 4, tzinfo=UTC),
                      target=date(2026, 10, 22))
    assert other_day.order_ids == []
    decisions = {
        row["decision"]
        for row in _rows(reopened, "SELECT decision FROM m1_evaluations WHERE target_date='2026-10-22'")
    }
    assert "HALTED" in decisions
    assert _halt_rules(reopened) == {"loss_stop"}
    # Settlement of the open lots still runs normally after a halt.
    assert reopened.settle_paper_orders(TARGET.isoformat(), 70.0, series_ticker="KXHIGHLAX") == 2


def test_admission_applies_the_loss_stop_before_any_daily_check(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _insert_settled_book(store, 30, pnl_each=-10.0)  # exactly -$300
    result = _scan(store)
    assert result.order_ids == []
    assert {reason for _, reason in result.blocked} == {"halted by stop rule(s): loss_stop"}
    assert _halt_rules(store) == {"loss_stop"}


def test_futility_stops_after_400_settled_entries_at_minus_five_percent(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _insert_settled_book(store, 399, pnl_each=-0.6)  # ROI -6.1%, -$239.40
    assert engine.evaluate_m1_stop_rules(store, now=NOW)["new_halts"] == []
    _insert_settled_book(store, 1, pnl_each=-0.6, offset=399)
    status = engine.evaluate_m1_stop_rules(store, now=NOW)
    assert status["new_halts"] == ["futility"]
    assert status["settled_entries"] == 400
    assert status["settled_roi"] == pytest.approx(-0.6 / 9.8)


def test_futility_does_not_fire_above_minus_five_percent(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _insert_settled_book(store, 400, pnl_each=-0.4)  # ROI -4.1%
    assert engine.evaluate_m1_stop_rules(store, now=NOW)["new_halts"] == []
    assert _halt_rules(store) == set()


def _insert_premise_evaluations(store: PaperStore, *, yes_counts: dict[float, int], dates: int) -> None:
    series_by_q = {
        0.1: "KXHIGHLAX", 0.3: "KXHIGHNY", 0.5: "KXHIGHCHI", 0.7: "KXHIGHMIA", 0.9: "KXHIGHAUS",
    }
    start = date(2026, 9, 1)
    with store.connect() as conn:
        for day_index in range(dates):
            target = start + timedelta(days=day_index)
            created = datetime(target.year, target.month, target.day, tzinfo=UTC) - timedelta(hours=5)
            for q, series in series_by_q.items():
                ticker = f"{series}-D{day_index:03d}-T70"
                conn.execute(
                    "INSERT INTO m1_evaluations (created_at, utc_hour, scan_id, policy_version, "
                    "policy_fingerprint, coefficient_set_id, target_date, market_ticker, "
                    "event_ticker, series_ticker, side, strike_type, floor_strike, civil_tz, "
                    "local_hour, lead_days, listing_hour, bucket, market_probability, q_yes, "
                    "ask, bid, spread_cents, cost, leg_passes, contracts, stake, "
                    "placement_enabled, decision) VALUES (?, ?, 'syn', ?, ?, ?, ?, ?, ?, ?, "
                    "'YES', 'greater', 70.0, 'America/Los_Angeles', 12.0, 1, 0, 'L1b', ?, ?, "
                    "?, ?, 2, ?, 0, 0, 0, 1, 'NO_SIGNAL')",
                    (
                        created.isoformat(), created.strftime("%Y-%m-%dT%H"),
                        m1.M1_POLICY.policy_version, m1.M1_POLICY.policy_fingerprint,
                        m1.M1_POLICY.coefficient_set_id, target.isoformat(), ticker,
                        ticker.rsplit("-", 1)[0], series, q, q, q + 0.01, q - 0.01,
                        q + 0.03,
                    ),
                )
                high = 75.0 if day_index < yes_counts[q] else 65.0
                conn.execute(
                    "INSERT INTO m1_settlement_truth (series_ticker, target_date, "
                    "settlement_high_f, truth_source, recorded_at) VALUES (?, ?, ?, 'test', ?) "
                    "ON CONFLICT DO NOTHING",
                    (series, target.isoformat(), high, created.isoformat()),
                )


def test_premise_pauses_when_the_slope_falls_below_1_05(tmp_path) -> None:
    store, _ = _store(tmp_path)
    # Outcomes exactly as often as the market said: slope 1.0, no recalibration
    # premise left.
    _insert_premise_evaluations(
        store, yes_counts={0.1: 3, 0.3: 9, 0.5: 15, 0.7: 21, 0.9: 27}, dates=30
    )
    result = engine.check_m1_premise(store, now=NOW)
    assert result["status"] == "triggered"
    assert result["slope"] == pytest.approx(1.0, abs=1e-6)
    assert result["settled_target_dates"] == 30
    assert _halt_rules(store) == {"premise"}
    assert _scan(store).order_ids == []


def test_premise_holds_while_favorites_win_more_than_priced(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _insert_premise_evaluations(
        store, yes_counts={0.1: 1, 0.3: 6, 0.5: 15, 0.7: 24, 0.9: 29}, dates=30
    )
    result = engine.check_m1_premise(store, now=NOW)
    assert result["status"] == "ok" and result["slope"] > 1.05
    assert _halt_rules(store) == set()


def test_premise_waits_for_thirty_settled_target_dates(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _insert_premise_evaluations(
        store, yes_counts={0.1: 3, 0.3: 9, 0.5: 15, 0.7: 21, 0.9: 27}, dates=29
    )
    result = engine.check_m1_premise(store, now=NOW)
    assert result["status"] == "insufficient_data"
    assert _halt_rules(store) == set()


def test_final_read_lets_the_sixtieth_trading_day_finish_then_stops(tmp_path) -> None:
    store, _ = _store(tmp_path)
    for index in range(59):
        target = date(2026, 8, 1) + timedelta(days=index)
        _insert_m1_order(
            store,
            target_date=target.isoformat(),
            ticker=f"KXHIGHLAX-FR{index:03d}-B70.5",
            created_at=datetime(target.year, target.month, target.day, tzinfo=UTC).isoformat(),
            realized_pnl=0.2,
        )
    assert len(_scan(store).order_ids) == 2  # the 60th trading day, both legs
    assert engine.evaluate_m1_stop_rules(store, now=NOW)["new_halts"] == []
    # 00:10 UTC on the 60th target date is still 17:10 PDT on D-1: Pacific
    # stations can still enter it, so the horizon stays open.
    assert engine.evaluate_m1_stop_rules(
        store, now=datetime(2026, 10, 21, 0, 10, tzinfo=UTC)
    )["new_halts"] == []
    assert engine.evaluate_m1_stop_rules(
        store, now=datetime(2026, 10, 21, 6, 59, tzinfo=UTC)
    )["new_halts"] == []
    later = engine.evaluate_m1_stop_rules(store, now=datetime(2026, 10, 21, 7, 0, tzinfo=UTC))
    assert later["new_halts"] == ["final_read"]
    assert later["trading_days"] == 60


def test_final_read_blocks_a_sixty_first_trading_day_at_admission(tmp_path) -> None:
    store, _ = _store(tmp_path)
    for index in range(60):
        target = date(2026, 8, 1) + timedelta(days=index)
        _insert_m1_order(
            store,
            target_date=target.isoformat(),
            ticker=f"KXHIGHLAX-FR{index:03d}-B70.5",
            created_at=datetime(target.year, target.month, target.day, tzinfo=UTC).isoformat(),
            realized_pnl=0.2,
        )
    result = _scan(store)
    assert result.order_ids == []
    assert {reason for _, reason in result.blocked} == {"halted by stop rule(s): final_read"}
    assert _halt_rules(store) == {"final_read"}


def test_final_read_stops_at_1000_entries(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _insert_settled_book(store, 1000, pnl_each=0.1, per_day=20)
    status = engine.evaluate_m1_stop_rules(store, now=NOW)
    assert status["new_halts"] == ["final_read"]
    assert _scan(store).order_ids == []


# --------------------------------------------------------------------------
# Daily reconciliation and the fidelity halt
# --------------------------------------------------------------------------


def test_reconciliation_matches_entries_from_the_durable_record_alone(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _scan(store)
    _scan(store, now=datetime(2026, 10, 20, 20, 3, tzinfo=UTC))
    with store.connect() as conn:
        conn.execute("DELETE FROM decision_snapshots")  # production prunes these
    report = engine.reconcile_m1_day(store, date(2026, 10, 20), now=NOW)
    assert report["evaluations"] == 12
    assert [entry["leg"] for entry in report["matched"]] == [list(YES_LEG), list(TAIL_LEG)]
    assert report["failed"] == report["missed"] == report["breaches"] == []
    assert report["contradictions"] == [] and report["recorded"] is True
    assert report["fidelity"]["triggered"] is False
    assert report["premise"]["status"] == "insufficient_data"
    again = engine.reconcile_m1_day(store, date(2026, 10, 20), now=NOW)
    assert again["recorded"] is False
    assert len(_rows(store, "SELECT * FROM m1_reconciliations")) == 1


def test_two_failed_matches_in_a_week_halt_new_entries(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _scan(store)
    # An M1 lot with no recorded evaluation behind it fails the replay match.
    _insert_m1_order(
        store, target_date="2026-10-21", ticker="KXHIGHLAX-26OCT21-B68.5",
        created_at=datetime(2026, 10, 20, 21, 30, tzinfo=UTC).isoformat(),
        status="PAPER_FILLED",
    )
    first = engine.reconcile_m1_day(store, date(2026, 10, 20), now=NOW)
    assert [row["issue"] for row in first["failed"]] == ["no replayed entry for this leg"]
    assert first["fidelity"]["triggered"] is False
    _insert_m1_order(
        store, target_date="2026-10-24", ticker="KXHIGHLAX-26OCT24-B68.5",
        created_at=datetime(2026, 10, 23, 21, 30, tzinfo=UTC).isoformat(),
        status="PAPER_FILLED",
    )
    third = engine.reconcile_m1_day(store, date(2026, 10, 23), now=NOW)
    assert third["fidelity"] == {
        "window_start": "2026-10-17",
        "window_end": "2026-10-23",
        "failed_matches": 2,
        "breaches": 0,
        "triggered": True,
        "halt_recorded": True,
    }
    assert _halt_rules(store) == {"fidelity"}


def test_failures_more_than_a_week_apart_do_not_halt(tmp_path) -> None:
    store, _ = _store(tmp_path)
    for day in (date(2026, 10, 10), date(2026, 10, 18)):
        _insert_m1_order(
            store, target_date=(day + timedelta(days=1)).isoformat(),
            ticker=f"KXHIGHLAX-X{day.day}-B68.5",
            created_at=datetime(day.year, day.month, day.day, 21, tzinfo=UTC).isoformat(),
            status="PAPER_FILLED",
        )
        report = engine.reconcile_m1_day(store, day, now=NOW)
        assert report["fidelity"]["triggered"] is False
    assert _halt_rules(store) == set()


def test_any_listing_hour_entry_is_an_immediate_fidelity_breach(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _insert_m1_order(
        store, target_date="2026-10-21", ticker="KXHIGHLAX-26OCT21-B66.5",
        created_at=datetime(2026, 10, 20, 14, 30, tzinfo=UTC).isoformat(),
        status="PAPER_FILLED",
    )
    report = engine.reconcile_m1_day(store, date(2026, 10, 20), now=NOW)
    assert {row["breach"] for row in report["breaches"]} == {"entry in the ET listing hour"}
    assert report["fidelity"]["triggered"] is True
    assert _halt_rules(store) == {"fidelity"}


def test_settle_maintenance_records_truth_reconciles_and_checks_stops(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _scan(store)
    store.settle_paper_orders(TARGET.isoformat(), 70.0, series_ticker="KXHIGHLAX")
    calls: list[str] = []

    def load_truth(city):
        calls.append(city.slug)
        return {TARGET: 70.0} if city.slug == "lax" else {}

    later = datetime(2026, 10, 22, 15, tzinfo=UTC)
    summary = engine.run_m1_settle_maintenance(store, load_truth=load_truth, now=later)
    assert calls == ["lax"]  # only series with recorded legs missing a final high
    assert summary["truth_recorded"] == 1
    assert [day["utc_day"] for day in summary["reconciled_days"]] == ["2026-10-20", "2026-10-21"]
    assert summary["stop_rules"]["new_halts"] == []
    again = engine.run_m1_settle_maintenance(store, load_truth=load_truth, now=later)
    assert again["reconciled_days"] == [] and again["truth_recorded"] == 0
    payload = engine.m1_trial_payload(store, now=later, resamples=200)
    five_minute = payload["shadow_arms"]["m1_5min"]
    assert five_minute["settled_entries"] == 2
    assert five_minute["pnl"] == pytest.approx(10.05 + 0.44)
    assert five_minute["paper_fills"] is False


def test_a_failed_truth_read_still_applies_the_ledger_stops(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _scan(store)  # a recorded leg with no final high, so load_truth must run
    _insert_settled_book(store, 400, pnl_each=-0.6, offset=10)

    def load_truth(city):
        raise RuntimeError("forecaster database unavailable")

    later = datetime(2026, 10, 22, 15, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="forecaster database unavailable"):
        engine.run_m1_settle_maintenance(store, load_truth=load_truth, now=later)

    assert _halt_rules(store) == {"futility"}
    again = engine.run_m1_settle_maintenance(store, load_truth=lambda city: {}, now=later)
    assert again["stop_rules"]["new_halts"] == []


def test_settle_maintenance_reports_a_halt_from_the_early_stop_check(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _insert_settled_book(store, 400, pnl_each=-0.6)
    summary = engine.run_m1_settle_maintenance(store, now=NOW)
    assert summary["stop_rules"]["new_halts"] == ["futility"]


def _forecaster_root(tmp: Path) -> Path:
    root = tmp / "forecaster"
    root.mkdir()
    with closing(sqlite3.connect(root / "weather.db")) as conn:
        city_truth.ensure_schema(conn)
        city_truth.upsert_settlement(conn, "KLAX", TARGET.isoformat(), 70.0, is_final=True)
        conn.commit()
    return root


def test_auto_settle_settles_m1_and_runs_its_daily_upkeep(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _scan(store)
    root = _forecaster_root(tmp_path)
    instant = datetime(2026, 10, 22, 7, 10, tzinfo=LAX.fixed_standard_timezone())
    out, err = io.StringIO(), io.StringIO()
    with patch(
        "sfo_kalshi_quant.cli.settlement_clock", lambda now=None, city=None: instant
    ), patch(
        "sfo_kalshi_quant.cli.settlement_today", lambda now=None, city=None: instant.date()
    ), redirect_stdout(out), redirect_stderr(err):
        code = main(
            [
                "--forecaster-root", str(root), "--db-path", str(tmp_path / "paper.db"),
                "--no-color", "paper-auto-settle", "--cities", "lax", "--skip-exchange-check",
            ]
        )
    assert code == 0, err.getvalue()
    assert {o["status"] for o in _m1_orders(store)} == {"PAPER_SETTLED"}
    assert "M1 trial reconciliation 2026-10-20: failed matches=0 breaches=0 missed=0" in out.getvalue()
    truth = _rows(store, "SELECT * FROM m1_settlement_truth")
    assert [(row["series_ticker"], row["target_date"], row["settlement_high_f"]) for row in truth] == [
        ("KXHIGHLAX", "2026-10-21", 70.0)
    ]


def test_trial_upkeep_failure_never_blocks_settlement(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _scan(store)
    root = _forecaster_root(tmp_path)
    instant = datetime(2026, 10, 22, 7, 10, tzinfo=LAX.fixed_standard_timezone())

    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    err = io.StringIO()
    with patch(
        "sfo_kalshi_quant.cli.settlement_clock", lambda now=None, city=None: instant
    ), patch(
        "sfo_kalshi_quant.cli.settlement_today", lambda now=None, city=None: instant.date()
    ), patch(
        "sfo_kalshi_quant._cli.paper.run_m1_settle_maintenance", explode
    ), redirect_stdout(io.StringIO()), redirect_stderr(err):
        code = main(
            [
                "--forecaster-root", str(root), "--db-path", str(tmp_path / "paper.db"),
                "--no-color", "paper-auto-settle", "--cities", "lax", "--skip-exchange-check",
            ]
        )
    assert code == 0
    assert {o["status"] for o in _m1_orders(store)} == {"PAPER_SETTLED"}
    assert "M1 trial maintenance failed (RuntimeError: boom)" in err.getvalue()


# --------------------------------------------------------------------------
# CLI and the research scan
# --------------------------------------------------------------------------


def test_placement_flag_is_explicit_research_only_and_default_off() -> None:
    parser = build_parser()
    default = parser.parse_args(["--risk-profile", "research", "portfolio-scan"])
    assert default.place_research_m1 is False
    assert scan_module._research_m1_placement_flag(default) is False
    flagged = parser.parse_args(["--risk-profile", "research", "portfolio-scan", "--place-research-m1"])
    assert scan_module._research_m1_placement_flag(flagged) is True
    assert scan_module._research_placement_flags(flagged) == (False, False)
    assert scan_module._paper_placement_requested(flagged) is True
    target_only = parser.parse_args(
        ["--risk-profile", "research", "portfolio-scan", "--place-paper", "--place-research-target"]
    )
    assert scan_module._research_m1_placement_flag(target_only) is False
    live = parser.parse_args(["--risk-profile", "live", "portfolio-scan", "--place-research-m1"])
    assert scan_module._research_m1_placement_flag(live) is False


def _context(markets, decisions, *, forecast=None, event=True):
    return SimpleNamespace(
        city=LAX,
        decisions=decisions,
        markets=markets,
        forecast=forecast or _forecast(),
        intraday=None,
        event=(
            EventSnapshot(event_ticker=EVENT, title="LAX", target_date=TARGET, markets=markets)
            if event
            else None
        ),
    )


def test_the_research_scan_feeds_the_trial_and_respects_the_flag(tmp_path) -> None:
    store, clock = _store(tmp_path)
    markets, decisions = _ladder()
    color = Color.from_no_color(True)
    shadow_args = SimpleNamespace(risk_profile="research", place_research_m1=False)
    with redirect_stdout(io.StringIO()):
        scan_module._run_research_m1_scan(shadow_args, TARGET, _context(markets, decisions), store, color)
    assert _m1_orders(store) == []
    # Placing in a fresh book (in this one the legs already crossed in shadow
    # mode and are never entered later).
    placing_store, _ = _store(tmp_path / "placing")
    place_args = SimpleNamespace(risk_profile="research", place_research_m1=True)
    out = io.StringIO()
    with redirect_stdout(out):
        scan_module._run_research_m1_scan(
            place_args, TARGET, _context(markets, decisions), placing_store, color
        )
    assert len(_m1_orders(placing_store)) == 2
    assert "M1 trial (placing) sampled 6/6 legs, 2 signal(s), 2 paper entries" in out.getvalue()

    # No listed ladder: nothing is evaluated at all.
    clock.now = NOW + timedelta(hours=2)
    before = len(_rows(store, "SELECT id FROM m1_evaluations"))
    scan_module._run_research_m1_scan(place_args, TARGET, _context(markets, decisions, event=False), store, color)
    assert len(_rows(store, "SELECT id FROM m1_evaluations")) == before


def test_a_single_source_forecast_blocks_m1_like_every_research_entry(tmp_path) -> None:
    store, _ = _store(tmp_path)
    markets, decisions = _ladder()
    args = SimpleNamespace(risk_profile="research", place_research_m1=True)
    with redirect_stdout(io.StringIO()):
        scan_module._run_research_m1_scan(
            args, TARGET, _context(markets, decisions, forecast=_forecast(source_count=1)),
            store, Color.from_no_color(True),
        )
    assert _m1_orders(store) == []
    reasons = {
        row["reason"] for row in _rows(store, "SELECT reason FROM m1_evaluations WHERE decision='RISK_BLOCKED'")
    }
    assert len(reasons) == 1 and "single-source forecast" in reasons.pop()


def test_a_trial_failure_never_stops_the_research_scan(tmp_path, monkeypatch) -> None:
    store, _ = _store(tmp_path)
    markets, decisions = _ladder()

    def explode(*args, **kwargs):
        raise RuntimeError("trial bug")

    monkeypatch.setattr(scan_module, "run_m1_scan", explode)
    err = io.StringIO()
    with redirect_stderr(err):
        scan_module._run_research_m1_scan(
            SimpleNamespace(risk_profile="research", place_research_m1=True),
            TARGET, _context(markets, decisions), store, Color.from_no_color(True),
        )
    assert "M1 trial evaluation failed (RuntimeError: trial bug)" in err.getvalue()
    assert "research target book is unaffected" in err.getvalue()


def test_status_and_reconcile_commands(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _scan(store)
    out = io.StringIO()
    with redirect_stdout(out):
        assert main(["--db-path", str(tmp_path / "paper.db"), "paper-m1-status"]) == 0
    status = json.loads(out.getvalue())
    assert status["account_id"] == M1 and status["book"]["entries"] == 2
    assert "2,000 resamples" not in status["inference_note"]
    assert "2000 resamples" in status["inference_note"]
    out = io.StringIO()
    with redirect_stdout(out):
        assert main(["--db-path", str(tmp_path / "paper.db"), "paper-m1-status", "--final-read"]) == 0
    assert "10000 resamples" in json.loads(out.getvalue())["inference_note"]
    out = io.StringIO()
    with redirect_stdout(out):
        code = main(
            ["--db-path", str(tmp_path / "paper.db"), "--no-color", "paper-m1-reconcile",
             "--day", "2026-10-20"]
        )
    assert code == 0
    assert len(json.loads(out.getvalue())["matched"]) == 2



# --------------------------------------------------------------------------
# Review fixes: admission errors, shadow crossings, quote time, monitor writes
# --------------------------------------------------------------------------


def test_a_locked_admission_never_becomes_a_false_fidelity_breach(tmp_path, monkeypatch) -> None:
    store, _ = _store(tmp_path)
    real = engine.admit_m1_entry
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    monkeypatch.setattr(engine, "admit_m1_entry", flaky)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        _scan(store)
    crossed = _rows(
        store,
        "SELECT decision, reason FROM m1_evaluations WHERE leg_passes=1 ORDER BY id",
    )
    # The failed candidate and every one after it leave PENDING behind.
    assert [row["decision"] for row in crossed] == ["ADMISSION_ERROR", "ADMISSION_ERROR"]
    assert all("database is locked" in row["reason"] for row in crossed)
    assert calls["n"] == 1 and _m1_orders(store) == []

    monkeypatch.setattr(engine, "admit_m1_entry", real)
    later = _scan(store, now=datetime(2026, 10, 20, 20, 3, tzinfo=UTC))
    assert len(later.order_ids) == 2
    report = engine.reconcile_m1_day(store, date(2026, 10, 20), now=NOW)
    assert report["failed"] == [] and report["breaches"] == [] and report["missed"] == []
    assert len(report["matched"]) == 2
    assert report["fidelity"]["triggered"] is False
    assert _halt_rules(store) == set()


def test_a_crash_between_commit_and_admission_is_not_a_fidelity_breach(
    tmp_path, monkeypatch
) -> None:
    store, _ = _store(tmp_path)

    def killed(*args, **kwargs):
        raise KeyboardInterrupt  # stands in for a kill: nothing gets to clean up

    monkeypatch.setattr(engine, "admit_m1_entry", killed)
    with pytest.raises(KeyboardInterrupt):
        _scan(store)
    assert {
        row["decision"]
        for row in _rows(store, "SELECT decision FROM m1_evaluations WHERE leg_passes=1")
    } == {"PENDING_ADMISSION"}
    monkeypatch.undo()
    assert len(_scan(store, now=datetime(2026, 10, 20, 20, 3, tzinfo=UTC)).order_ids) == 2
    report = engine.reconcile_m1_day(store, date(2026, 10, 20), now=NOW)
    assert report["failed"] == [] and report["missed"] == []
    assert len(report["pending_admissions"]) == 2
    assert _halt_rules(store) == set()


def test_the_scan_reraises_a_sqlite_lock_so_the_tick_retries(tmp_path, monkeypatch) -> None:
    store, _ = _store(tmp_path)
    markets, decisions = _ladder()

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(scan_module, "run_m1_scan", locked)
    with pytest.raises(sqlite3.OperationalError), redirect_stderr(io.StringIO()):
        scan_module._run_research_m1_scan(
            SimpleNamespace(risk_profile="research", place_research_m1=True),
            TARGET, _context(markets, decisions), store, Color.from_no_color(True),
        )

    # Any other sqlite error in M1 stays contained: V7 keeps scanning.
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("no such column: m1_bogus")

    monkeypatch.setattr(scan_module, "run_m1_scan", broken)
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        scan_module._run_research_m1_scan(
            SimpleNamespace(risk_profile="research", place_research_m1=True),
            TARGET, _context(markets, decisions), store, Color.from_no_color(True),
        )
    assert "the research target book is unaffected" in stderr.getvalue()


def test_turning_placement_on_never_enters_a_leg_that_crossed_in_shadow(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _scan(store, place=False)
    shadow_again = _scan(store, place=False, now=datetime(2026, 10, 20, 20, 4, tzinfo=UTC))
    assert shadow_again.sampled == 6
    placing = _scan(store, place=True, now=datetime(2026, 10, 20, 21, 4, tzinfo=UTC))
    assert placing.order_ids == [] and placing.candidates == 0
    by_hour = {
        (row["utc_hour"], row["market_ticker"]): (row["decision"], row["reason"])
        for row in _rows(store, "SELECT * FROM m1_evaluations WHERE leg_passes=1")
    }
    assert by_hour[("2026-10-20T19", YES_TICKER)][0] == "SHADOW"
    for hour in ("2026-10-20T20", "2026-10-20T21"):
        decision, reason = by_hour[(hour, YES_TICKER)]
        assert decision == "ALREADY_CROSSED"
        assert reason == "first crossing already recorded in shadow mode"
    report = engine.reconcile_m1_day(store, date(2026, 10, 20), now=NOW)
    assert report["expected_entries"] == 0
    assert report["failed"] == report["missed"] == report["contradictions"] == []

    # A leg whose first crossing comes after the switch is entered as usual.
    next_day = _scan(
        store, now=datetime(2026, 10, 21, 19, 4, tzinfo=UTC), target=date(2026, 10, 22)
    )
    assert len(next_day.order_ids) == 2


def test_m1_rows_carry_the_time_the_scan_read_its_quotes(tmp_path) -> None:
    store, clock = _store(tmp_path)
    # The quotes were read just before 19:00 UTC; the target book's placement
    # and depth budget pushed M1's turn past the hour.
    clock.now = datetime(2026, 10, 20, 19, 0, 20, tzinfo=UTC)
    quoted_at = datetime(2026, 10, 20, 18, 59, 58, tzinfo=UTC)
    markets, decisions = _ladder()
    context = _context(markets, decisions)
    context.quote_observed_at = quoted_at
    with redirect_stdout(io.StringIO()):
        scan_module._run_research_m1_scan(
            SimpleNamespace(risk_profile="research", place_research_m1=True),
            TARGET, context, store, Color.from_no_color(True),
        )
    rows = _rows(store, "SELECT created_at, utc_hour, local_hour FROM m1_evaluations")
    assert {row["utc_hour"] for row in rows} == {"2026-10-20T18"}
    assert {row["created_at"] for row in rows} == {quoted_at.isoformat()}
    assert {o["created_at"] for o in _m1_orders(store)} == {quoted_at.isoformat()}


def test_build_scan_context_records_when_it_read_the_event(monkeypatch) -> None:
    assert "quote_observed_at" in {
        field.name for field in scan_module.ScanContext.__dataclass_fields__.values()
    }
    source = Path(scan_module.__file__).read_text(encoding="utf-8")
    assert "quote_observed_at=quote_observed_at" in source


def test_the_monitor_records_the_hold_once_per_lot(tmp_path) -> None:
    store, _ = _store(tmp_path)
    order_ids = _scan(store).order_ids

    class NoQuotes:
        def get_market(self, ticker):  # pragma: no cover -- never called for M1
            raise AssertionError("an M1 lot never needs a quote")

    for _ in range(3):
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", NoQuotes), redirect_stdout(io.StringIO()):
            assert main(["--db-path", str(tmp_path / "paper.db"), "--no-color", "paper-monitor"]) == 0
    snapshots = _rows(
        store,
        "SELECT order_id, action FROM paper_monitor_snapshots ORDER BY id",
    )
    assert sorted(row["order_id"] for row in snapshots) == sorted(order_ids)
    assert {row["action"] for row in snapshots} == {"HOLD_TO_SETTLEMENT"}


def test_settle_maintenance_drains_a_long_backlog_oldest_days_first(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _scan(store)
    first_day = date.fromisoformat(
        _rows(store, "SELECT MIN(created_at) AS at FROM m1_evaluations")[0]["at"][:10]
    )
    resumed = datetime(
        first_day.year, first_day.month, first_day.day, 15, tzinfo=UTC
    ) + timedelta(days=20)

    first = engine.run_m1_settle_maintenance(store, now=resumed)
    second = engine.run_m1_settle_maintenance(store, now=resumed)

    days = [
        (first_day + timedelta(days=offset)).isoformat() for offset in range(20)
    ]
    assert [row["utc_day"] for row in first["reconciled_days"]] == days[:14]
    assert [row["utc_day"] for row in second["reconciled_days"]] == days[14:]
