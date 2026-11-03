"""M1's paper book never changes Live Stability or Research V7 sizing or exits.

M1 rows carry ``risk_profile='research'`` because execution reuses the research
config, so every profile-wide query must leave the M1 account out unless the
caller asks for M1's own account.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sfo_kalshi_quant import monitor
from sfo_kalshi_quant._cli.scan import _sizing_bankroll
from sfo_kalshi_quant.account import LIVE_STABILITY_ACCOUNT_ID
from sfo_kalshi_quant.config import strategy_config_for_profile
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.exits import ExitSignal
from sfo_kalshi_quant.posterior_kelly import load_posterior_kelly_model
from sfo_kalshi_quant.research_m1 import M1_ACCOUNT_ID, M1_POLICY
from sfo_kalshi_quant.research_policy import TARGET_POLICY

DAY = "2026-10-21"
V7 = TARGET_POLICY.account_id


def _order(store: PaperStore, *, account_id: str, profile: str = "research", **cols) -> int:
    policy = M1_POLICY if account_id == M1_ACCOUNT_ID else TARGET_POLICY
    identity = (
        {
            "research_sleeve": getattr(policy.sleeve, "value", policy.sleeve),
            "research_policy_version": policy.policy_version,
            "policy_fingerprint": policy.policy_fingerprint,
        }
        if profile == "research"
        else {}
    )
    row = {
        "created_at": f"{DAY}T18:00:00+00:00",
        "target_date": DAY,
        "market_ticker": "KXHIGHLAX-26OCT21-B70.5",
        "label": "synthetic",
        "action": "BUY_YES",
        "risk_profile": profile,
        "side": "YES",
        "contracts": 10.0,
        "yes_ask": 0.5,
        "fee_per_contract": 0.0,
        "cost_per_contract": 0.5,
        "probability": 0.6,
        "probability_lcb": 0.6,
        "edge": 0.1,
        "edge_lcb": 0.1,
        "expected_profit": 1.0,
        "status": "PAPER_FILLED",
        "reasons_json": "[]",
        "account_id": account_id,
        "strike_type": "between",
        "floor_strike": 70,
        "cap_strike": 71,
        **identity,
        **cols,
    }
    with store.connect() as conn:
        cursor = conn.execute(
            f"INSERT INTO paper_orders ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
            tuple(row.values()),
        )
        return int(cursor.lastrowid)


def _settled(**cols) -> dict:
    return {
        "status": "PAPER_SETTLED",
        "settled_at": f"{DAY}T23:00:00+00:00",
        "resolved_yes": 1,
        "settlement_high_f": 70.0,
        "realized_pnl": 5.0,
        **cols,
    }


def _kelly(store: PaperStore):
    with store.connect() as conn:
        return load_posterior_kelly_model(conn)


def test_m1_lots_leave_the_live_and_research_kelly_model_unchanged(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    _order(store, account_id=LIVE_STABILITY_ACCOUNT_ID, profile="live", **_settled())
    _order(store, account_id=V7, market_ticker="KXHIGHLAX-26OCT21-B72.5", **_settled())
    # A V7 early exit on a date only M1 settled must stay unscored.
    _order(
        store,
        account_id=V7,
        target_date="2026-10-22",
        market_ticker="KXHIGHLAX-26OCT22-B70.5",
        status="PAPER_CLOSED",
        closed_at="2026-10-22T20:00:00+00:00",
        realized_pnl=-1.0,
    )
    before = _kelly(store)

    _order(store, account_id=M1_ACCOUNT_ID, market_ticker="KXHIGHLAX-26OCT21-B74.5",
           **_settled(resolved_yes=0, realized_pnl=-5.0))
    _order(store, account_id=M1_ACCOUNT_ID, target_date="2026-10-22",
           market_ticker="KXHIGHLAX-26OCT22-B72.5", **_settled(settlement_high_f=70.0))
    _order(store, account_id=M1_ACCOUNT_ID, market_ticker="KXHIGHLAX-26OCT21-B76.5",
           status="PAPER_CLOSED", closed_at=f"{DAY}T20:00:00+00:00", realized_pnl=-2.0)

    assert _kelly(store) == before


def test_m1_realized_pnl_leaves_the_research_sizing_bankroll_unchanged(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    config = strategy_config_for_profile("research")
    assert config.size_against_live_equity
    _order(store, account_id=V7, **_settled(realized_pnl=40.0))
    before = _sizing_bankroll(store, config, "research")

    _order(store, account_id=M1_ACCOUNT_ID, market_ticker="KXHIGHLAX-26OCT21-B74.5",
           **_settled(realized_pnl=-250.0))

    assert _sizing_bankroll(store, config, "research") == before
    # M1's own book still sees its P&L when it asks for its own account.
    assert store.paper_equity(10_000.0, risk_profile="research", account_id=M1_ACCOUNT_ID) == 9_750.0


def test_an_open_m1_no_lot_does_not_join_the_v7_no_basket_veto(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    v7_id = _order(store, account_id=V7, side="NO", action="BUY_NO")

    def veto() -> str | None:
        with store.connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM paper_orders WHERE id=?", (v7_id,)).fetchone()
        return monitor._same_day_no_basket_veto_reason(
            store,
            row,
            side="NO",
            signal=ExitSignal(action="STOP_LOSS", reason="stop"),
            entry_cost=0.5,
            net_exit=0.3,
            model_side_probability=0.9,
            model_veto_buffer=0.05,
        )

    assert veto() is None  # a one-leg basket never holds
    _order(store, account_id=M1_ACCOUNT_ID, side="NO", action="BUY_NO",
           market_ticker="KXHIGHLAX-26OCT21-B74.5")

    assert veto() is None
    assert len(store.open_no_basket_orders(DAY, risk_profile="research")) == 1
    assert len(store.open_no_basket_orders(DAY, risk_profile="research", account_id=M1_ACCOUNT_ID)) == 1
