"""Partial exits must not reset a logical position's dollar loss guard."""

from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from unittest.mock import patch

import pytest

from sfo_kalshi_quant.cli import main
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import MarketBin, TradeDecision
from sfo_kalshi_quant.research_policy import TARGET_POLICY


TARGET_DATE = "2026-09-12"
TICKER = "KXHIGHTSFO-26SEP12-B82.5"


def _seed_target_position(db_path, *, contracts=100.0, ticker=TICKER):
    store = PaperStore(db_path)
    decision = TradeDecision(
        ticker=ticker,
        label="82° or above",
        action="BUY_NO",
        approved=True,
        probability=0.9,
        probability_lcb=0.85,
        yes_bid=0.19,
        yes_ask=0.21,
        spread=0.02,
        fee_per_contract=0.013,
        cost_per_contract=0.803,
        edge=0.097,
        edge_lcb=0.047,
        kelly_fraction=0.01,
        recommended_contracts=contracts,
        expected_profit=contracts * 0.097,
        reasons=[],
        side="NO",
        entry_bid=0.77,
        entry_ask=0.79,
        strike_type="above",
        floor_strike=82.0,
    )
    # Seed a genuine filled root/ledger, then attribute it to the research
    # target policy whose dollar safety floor is under test. Entry admission
    # is unrelated to this monitor lifecycle regression.
    order_id = store.record_paper_order(TARGET_DATE, decision, risk_profile="live")
    assert order_id is not None
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_orders SET account_id=?, risk_profile='research', "
            "research_sleeve=?, research_policy_version=?, policy_fingerprint=? "
            "WHERE id=?",
            (
                TARGET_POLICY.account_id,
                TARGET_POLICY.sleeve.value,
                TARGET_POLICY.policy_version,
                TARGET_POLICY.policy_fingerprint,
                order_id,
            ),
        )
        conn.execute(
            "UPDATE paper_account_ledger SET account_id=? WHERE order_id=?",
            (TARGET_POLICY.account_id, order_id),
        )
    return order_id


def _run_monitor(db_path, *, depth=10.0, model_probability=0.1, dry_run=False):
    class Client:
        def get_market(self, ticker):
            return MarketBin(
                ticker=ticker,
                event_ticker="KXHIGHTSFO-26SEP12",
                title="Highest temperature in San Francisco?",
                yes_sub_title="82° or above",
                strike_type="above",
                floor_strike=82.0,
                cap_strike=None,
                yes_bid=0.53,
                yes_ask=0.55,
                no_bid=0.45,
                no_ask=0.47,
                yes_bid_size=10.0,
                yes_ask_size=depth,
                status="active",
            )

    read = (
        None
        if model_probability is None
        else (datetime.now(UTC), model_probability)
    )
    with (
        patch("sfo_kalshi_quant.cli.KalshiPublicClient", Client),
        patch.object(PaperStore, "latest_model_probability_read", return_value=read),
        redirect_stdout(StringIO()),
    ):
        # Every CLI invocation constructs a new PaperStore, as separate monitor
        # processes do. The loss history must survive that boundary.
        args = ["--db-path", str(db_path), "--no-color", "paper-monitor"]
        if dry_run:
            args.append("--dry-run")
        assert main(args) == 0


def _latest_snapshot(db_path, order_id):
    with PaperStore(db_path).connect() as conn:
        return conn.execute(
            "SELECT action, reason, unrealized_pnl FROM paper_monitor_snapshots "
            "WHERE order_id=? ORDER BY id DESC LIMIT 1",
            (order_id,),
        ).fetchone()


@pytest.mark.parametrize("model_probability", [0.1, None])
def test_dollar_stop_continues_after_partial_close_and_monitor_restart(
    tmp_path, model_probability
):
    db_path = tmp_path / "paper.db"
    order_id = _seed_target_position(db_path)

    for _ in range(2):
        _run_monitor(db_path, model_probability=model_probability)

    action, reason, unrealized = _latest_snapshot(db_path, order_id)
    assert action == "CLOSE_STOP_LOSS"
    assert "veto dollar floor" in reason
    # Keep the published unrealized mark scoped to the remaining contracts;
    # only the decision's logical loss includes already realized slices.
    assert -35.0 < unrealized < 0.0
    store = PaperStore(db_path)
    assert store.open_paper_order(order_id)["contracts"] == 80.0
    with store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE parent_order_id=?",
            (order_id,),
        ).fetchone()[0] == 2


def test_partial_dollar_stop_waits_for_depth_and_resumes_without_reset(tmp_path):
    db_path = tmp_path / "paper.db"
    order_id = _seed_target_position(db_path)
    _run_monitor(db_path)
    _run_monitor(db_path, depth=0.0)
    assert _latest_snapshot(db_path, order_id)[0] == "HOLD_NO_DISPLAYED_DEPTH"
    assert PaperStore(db_path).open_paper_order(order_id)["contracts"] == 90.0

    for _ in range(9):
        _run_monitor(db_path)

    store = PaperStore(db_path)
    assert store.open_paper_order(order_id) is None
    with store.connect() as conn:
        quantity, count = conn.execute(
            "SELECT SUM(contracts), COUNT(*) FROM paper_orders "
            "WHERE id=? OR parent_order_id=?",
            (order_id, order_id),
        ).fetchone()
    assert quantity == 100.0
    assert count == 10


def test_partial_dollar_stop_dry_run_preserves_remaining_position(tmp_path):
    db_path = tmp_path / "paper.db"
    order_id = _seed_target_position(db_path)
    _run_monitor(db_path)
    _run_monitor(db_path, dry_run=True)
    assert _latest_snapshot(db_path, order_id)[0] == "WOULD_CLOSE"
    assert PaperStore(db_path).open_paper_order(order_id)["contracts"] == 90.0


def test_small_position_still_retains_model_veto(tmp_path):
    db_path = tmp_path / "paper.db"
    order_id = _seed_target_position(db_path, contracts=10.0)
    _run_monitor(db_path)
    assert _latest_snapshot(db_path, order_id)[0] == "HOLD_MODEL_VETO"
    assert PaperStore(db_path).open_paper_order(order_id)["contracts"] == 10.0


def test_persisted_partial_pnl_includes_both_profits_and_losses(tmp_path):
    db_path = tmp_path / "paper.db"
    order_id = _seed_target_position(db_path)
    store = PaperStore(db_path)
    assert store.partial_close_realized_pnl(order_id) == 0.0
    loss = store.close_paper_order(order_id, 0.45, max_quantity=10.0)
    profit = store.close_paper_order(order_id, 0.90, max_quantity=20.0)
    assert loss["realized_pnl"] < 0.0 < profit["realized_pnl"]
    restarted = PaperStore(db_path)
    assert restarted.partial_close_realized_pnl(order_id) == pytest.approx(
        loss["realized_pnl"] + profit["realized_pnl"]
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_id", "unrelated-paper-account"),
        ("policy_fingerprint", "unrelated-policy"),
        ("realized_pnl", None),
        ("cost_per_contract", 0.1),
    ],
)
def test_partial_pnl_rejects_corrupt_or_unrelated_child_evidence(
    tmp_path, field, value
):
    db_path = tmp_path / "paper.db"
    order_id = _seed_target_position(db_path)
    store = PaperStore(db_path)
    child = store.close_paper_order(order_id, 0.45, max_quantity=10.0)
    with store.connect() as conn:
        conn.execute(
            f"UPDATE paper_orders SET {field}=? WHERE id=?",
            (value, child["id"]),
        )
    with pytest.raises(ValueError, match="invalid partial-close loss evidence"):
        PaperStore(db_path).partial_close_realized_pnl(order_id)


def test_malformed_root_evidence_does_not_abort_tick_or_next_position_stop(
    tmp_path, capsys
):
    db_path = tmp_path / "paper.db"
    # The healthy root is created first; the monitor walks open orders newest
    # first, so the malformed root is processed BEFORE it and must not stop
    # the loop from reaching it.
    healthy_id = _seed_target_position(db_path)
    malformed_id = _seed_target_position(
        db_path, ticker="KXHIGHTSFO-26SEP12-B84.5"
    )
    store = PaperStore(db_path)
    child = store.close_paper_order(malformed_id, 0.45, max_quantity=10.0)
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_orders SET realized_pnl=NULL WHERE id=?", (child["id"],)
        )
        conn.execute(
            "UPDATE paper_orders SET created_at='2026-09-12T20:00:00+00:00' "
            "WHERE id=?",
            (healthy_id,),
        )
        conn.execute(
            "UPDATE paper_orders SET created_at='2026-09-12T21:00:00+00:00' "
            "WHERE id=?",
            (malformed_id,),
        )
    with pytest.raises(ValueError, match="invalid partial-close loss evidence"):
        PaperStore(db_path).partial_close_realized_pnl(malformed_id)

    # The whole tick used to raise here. It must complete, and the healthy
    # position's veto-dollar-floor stop must still fire on the same tick.
    _run_monitor(db_path)
    assert "partial-close loss evidence unavailable" in capsys.readouterr().err

    healthy_action, healthy_reason, _ = _latest_snapshot(db_path, healthy_id)
    assert healthy_action == "CLOSE_STOP_LOSS"
    assert "veto dollar floor" in healthy_reason
    assert PaperStore(db_path).open_paper_order(healthy_id)["contracts"] == 90.0

    # The malformed root is still managed with the pre-#121 input: the
    # dollar floor applies to the open remainder alone (90 contracts,
    # -$33.33, above the -$35 floor) and only the child-lot memory is lost
    # (with it the loss would be -$37.04 and the floor would bind). The model
    # veto therefore holds it (p=0.9 on the NO side), and the snapshot
    # carries the evidence error so the corruption is auditable.
    malformed_action, malformed_reason, _ = _latest_snapshot(db_path, malformed_id)
    assert malformed_action == "HOLD_MODEL_VETO"
    assert "invalid partial-close loss evidence" in malformed_reason
    assert "veto dollar floor applied to the open remainder only" in malformed_reason
    assert PaperStore(db_path).open_paper_order(malformed_id)["contracts"] == 90.0


def test_malformed_root_dollar_floor_still_fires_on_open_remainder(tmp_path):
    # Fail-safe direction of the degradation: when the open remainder alone
    # already breaches the $35 veto dollar floor (110 contracts, about
    # -$40.7), malformed child-lot evidence must not disable the floor and
    # let the model veto hold a catastrophic loss.
    db_path = tmp_path / "paper.db"
    order_id = _seed_target_position(db_path, contracts=120.0)
    store = PaperStore(db_path)
    child = store.close_paper_order(order_id, 0.45, max_quantity=10.0)
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_orders SET realized_pnl=NULL WHERE id=?", (child["id"],)
        )
    with pytest.raises(ValueError, match="invalid partial-close loss evidence"):
        PaperStore(db_path).partial_close_realized_pnl(order_id)

    _run_monitor(db_path)
    action, reason, _ = _latest_snapshot(db_path, order_id)
    assert action == "CLOSE_STOP_LOSS"
    assert "breached the $35.00 veto dollar floor" in reason
    assert "invalid partial-close loss evidence" in reason
    assert "veto dollar floor applied to the open remainder only" in reason
    assert PaperStore(db_path).open_paper_order(order_id)["contracts"] == 100.0


def test_malformed_root_ordinary_stop_still_fires_when_model_agrees(tmp_path):
    # The child-lot memory is unavailable and the open remainder (-$33.33)
    # does not breach the $35 floor, but the ordinary stop does not need
    # either: a model read that no longer supports the NO side (yes p=0.9)
    # lets the plain stop fire on the malformed root itself.
    db_path = tmp_path / "paper.db"
    order_id = _seed_target_position(db_path)
    store = PaperStore(db_path)
    child = store.close_paper_order(order_id, 0.45, max_quantity=10.0)
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_orders SET account_id='unrelated-paper-account' WHERE id=?",
            (child["id"],),
        )
    _run_monitor(db_path, model_probability=0.9)
    action, reason, _ = _latest_snapshot(db_path, order_id)
    assert action == "CLOSE_STOP_LOSS"
    assert "breached the" not in reason
    assert "invalid partial-close loss evidence" in reason
    assert "veto dollar floor applied to the open remainder only" in reason
    assert PaperStore(db_path).open_paper_order(order_id)["contracts"] == 80.0
