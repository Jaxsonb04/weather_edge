"""M1 market-recalibration paper trial: the published paper-only book."""

from __future__ import annotations

import pytest

from sfo_kalshi_quant import research_m1 as m1
from sfo_kalshi_quant import research_m1_store as engine
from sfo_kalshi_quant.account import LIVE_STABILITY_ACCOUNT_ID
from sfo_kalshi_quant.strategy_lab.build import (
    _accounting_payload,
    _bind_accounting_to_profiles,
)
from sfo_kalshi_quant.strategy_lab.profiles import _profile_label

from test_research_m1_trial import (
    M1,
    NOW,
    TARGET,
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


def test_the_m1_profile_has_its_own_label() -> None:
    assert _profile_label("research-m1") == "M1 market-recalibration trial (paper only)"


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



def test_the_m1_profile_reads_its_balances_from_its_own_book(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _scan(store)
    store.settle_paper_orders(TARGET.isoformat(), 70.0, series_ticker="KXHIGHLAX")
    accounting = _accounting_payload(
        {"days": [{"date": "2026-10-21"}]}, {"profiles": []}, db_path=tmp_path / "paper.db"
    )
    profiles = [
        {"risk_profile": name, "daily_summary": {"bankroll": 1000.0}}
        for name in ("research-target-v6", "research-m1", "research-target", "live")
    ]
    bound = _bind_accounting_to_profiles(profiles, accounting)
    # Active books first (live, Research V7, the M1 trial), archives after.
    assert [row["risk_profile"] for row in bound][:3] == [
        "live", "research-target", "research-m1",
    ]
    assert all(row.get("archived") is True for row in bound[3:])
    m1_row = bound[2]
    assert m1_row["account_key"] == "research_m1" and m1_row["archived"] is False
    assert m1_row["daily_summary"]["bankroll"] == 10_000.0
    assert m1_row["daily_summary"]["current_equity"] == pytest.approx(10_010.49)
    assert m1_row["daily_summary"]["equity_basis"] == "account_equity"
