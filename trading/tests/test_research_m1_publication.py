"""M1 market-recalibration paper trial: the published paper-only book."""

from __future__ import annotations

import pytest

from sfo_kalshi_quant import research_m1 as m1
from sfo_kalshi_quant import research_m1_store as engine
from sfo_kalshi_quant.account import LIVE_STABILITY_ACCOUNT_ID
from sfo_kalshi_quant.strategy_lab.build import (
    _accounting_payload,
)
from sfo_kalshi_quant.strategy_lab.paper_card import _paper_payload
from sfo_kalshi_quant.summary import _load_orders

from test_research_m1_trial import (
    M1,
    NOW,
    TARGET,
    _insert_m1_order,
    _insert_settled_book,
    _scan,
    _store,
)


def test_publication_shows_m1_as_its_own_paper_only_book(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _scan(store)
    store.settle_paper_orders(TARGET.isoformat(), 70.0, series_ticker="KXHIGHLAX")
    payload = _accounting_payload(
        {"days": [{"date": "2026-10-20"}, {"date": "2026-10-21"}]},
        {"profiles": []},
        db_path=tmp_path / "paper.db",
    )
    assert payload["available"] is True
    assert set(payload["active_ledgers"]) == {"live_stability", "research_roi"}
    assert payload["accounts"]["live"]["account_id"] == LIVE_STABILITY_ACCOUNT_ID
    assert M1 in payload["research_accounts_excluded_from_live_goal_and_readiness"]
    book = payload["accounts"]["research_m1"]
    assert book["available"] is True and book["account_id"] == M1
    assert book["profile_key"] == "research-m1"
    assert book["initial_equity"] == 10_000.0
    assert book["realized_pnl"] == pytest.approx(10.49)
    assert book["paper_only"] is True and book["real_money_eligible"] is False
    assert "live_readiness" in book["excluded_from"]
    trial = book["trial"]
    assert trial["kind"] == "market_recalibration_trial"
    assert trial["counts_toward_live_readiness"] is False
    assert trial["policy_fingerprint"] == m1.M1_POLICY.policy_fingerprint
    assert trial["caps"]["target_date_cap_dollars"] == 500.0
    assert trial["caps"]["cumulative_loss_stop_dollars"] == -300.0
    assert [rule["rule"] for rule in trial["stop_rules"]] == [
        "fidelity", "loss_stop", "futility", "premise", "final_read",
    ]
    assert {rule["status"] for rule in trial["stop_rules"]} <= {
        "ok", "not_yet_eligible", "not_yet_checked", "running",
    }
    assert [h["id"] for h in trial["hypotheses"]] == ["H1", "H2a", "H2b"]
    assert [h["alpha"] for h in trial["hypotheses"]] == [0.025, 0.0125, 0.0125]
    assert trial["hypotheses"][0]["settled_entries"] == 2
    assert trial["hypotheses"][1]["settled_entries"] == 1  # the agreeing YES leg
    assert trial["book"]["entries"] == 2
    assert trial["entries_enabled_by_stop_rules"] is True
    assert set(trial["shadow_arms"]) == {"m1_5min", "mv_hourly"}


def test_publication_reports_halts_without_hiding_other_accounts(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _insert_settled_book(store, 31, pnl_each=-10.0)
    engine.evaluate_m1_stop_rules(store, now=NOW)
    payload = _accounting_payload({"days": []}, {"profiles": []}, db_path=tmp_path / "paper.db")
    assert payload["available"] is True
    trial = payload["accounts"]["research_m1"]["trial"]
    assert trial["entries_enabled_by_stop_rules"] is False
    assert [halt["rule"] for halt in trial["halts"]] == ["loss_stop"]
    loss = next(rule for rule in trial["stop_rules"] if rule["rule"] == "loss_stop")
    assert loss["status"] == "halted" and loss["realized_pnl"] == -310.0


def test_an_unreadable_m1_book_publishes_as_unavailable_without_hiding_others(
    tmp_path, monkeypatch
) -> None:
    store, _ = _store(tmp_path)
    from sfo_kalshi_quant.strategy_lab import build

    def explode(*args, **kwargs):
        raise ZeroDivisionError("bad trial read")

    monkeypatch.setattr(build, "m1_trial_payload", explode)
    payload = _accounting_payload({"days": []}, {"profiles": []}, db_path=tmp_path / "paper.db")
    assert payload["available"] is True
    assert payload["accounts"]["live"]["account_id"] == LIVE_STABILITY_ACCOUNT_ID
    book = payload["accounts"]["research_m1"]
    assert book["available"] is False
    assert book["reason"] == "M1 trial ledger unavailable (ZeroDivisionError)"


def _insert_live_open(store, index: int) -> None:
    """An open Live Stability lot older than every M1 lot."""

    created = f"2026-10-19T12:{index:02d}:00+00:00"
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO paper_orders (
                created_at, target_date, market_ticker, label, action, risk_profile,
                side, contracts, yes_ask, entry_price, entry_bid, fee_per_contract,
                cost_per_contract, probability, probability_lcb, edge, edge_lcb,
                trade_quality_score, expected_profit, status, reasons_json,
                account_id, filled_contracts
            ) VALUES (?, '2026-10-21', ?, 'live lot', 'BUY_YES', 'live', 'YES', 5,
                      0.5, 0.5, 0.48, 0, 0.5, 0.6, 0.5, 0.1, 0.1, 0, 0.0,
                      'PAPER_FILLED', '[]', ?, 5)
            """,
            (created, f"KXHIGHLAX-LIVE{index}-B70.5", LIVE_STABILITY_ACCOUNT_ID),
        )


def test_m1_lots_never_enter_the_shared_paper_lists(tmp_path) -> None:
    store, _ = _store(tmp_path)
    for index in range(3):
        _insert_live_open(store, index)
    before = _paper_payload(tmp_path / "paper.db")
    for index in range(35):  # more than the 30-row open list, all newer than Live
        _insert_m1_order(
            store,
            target_date="2026-10-21",
            ticker=f"KXHIGHLAX-M1{index:03d}-B70.5",
            created_at=f"2026-10-20T13:{index:02d}:00+00:00",
            status="PAPER_FILLED",
        )
    paper = _paper_payload(tmp_path / "paper.db")
    assert paper["open_positions"] == before["open_positions"]
    assert len(paper["open_positions"]) == 3
    assert {row["account_id"] for row in paper["open_positions"]} == {LIVE_STABILITY_ACCOUNT_ID}
    for key in ("open_positions", "published_open_positions", "hidden_open_positions", "open_risk"):
        assert paper["summary"][key] == before["summary"][key]
    assert "research-m1" not in {row["risk_profile"] for row in paper["profiles"]}
    assert {row["label"] for row in paper["recent_monitor_actions"]} == {"live lot"}
    assert [row["risk_profile"] for row in _load_orders(tmp_path / "paper.db")] == ["live"] * 3
