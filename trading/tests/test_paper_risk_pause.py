from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.paper import PaperTrader
from sfo_kalshi_quant.config import strategy_config_for_profile
from sfo_kalshi_quant.research_policy import MOTION_POLICY, TARGET_POLICY


def _decision(ticker: str = "KXHIGHTSFO-TEST-B70.5") -> TradeDecision:
    return TradeDecision(
        ticker=ticker,
        label="70° to 71°",
        action="BUY_YES",
        approved=True,
        probability=0.70,
        probability_lcb=0.62,
        yes_bid=0.48,
        yes_ask=0.50,
        spread=0.02,
        fee_per_contract=0.02,
        cost_per_contract=0.52,
        edge=0.18,
        edge_lcb=0.10,
        kelly_fraction=0.01,
        recommended_contracts=10.0,
        expected_profit=1.8,
        reasons=[],
        trade_quality_score=80.0,
        strike_type="between",
        floor_strike=70.0,
        cap_strike=71.0,
    )


def test_research_pauses_after_five_bad_resolved_trades():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        for idx in range(5):
            order_id = store.record_paper_order(
                "2026-06-12",
                _decision(f"KXHIGHTSFO-TEST-B{70 + idx}.5"),
                risk_profile="research",
            )
            store.close_paper_order(order_id, 0.01)

        reason = store.paper_entry_pause_reason(
            "research",
            bankroll=1000.0,
            target_date="2026-06-13",
        )

        assert reason is not None
        assert "research paused" in reason
        assert "resolved ROI" in reason
        trader = PaperTrader(
            store,
            strategy_config_for_profile("research"),
            risk_profile="research",
        )
        assert trader.place_approved("2026-06-13", [_decision()], bankroll=1000.0) == []


def test_partial_exit_lots_do_not_count_as_resolved_trades():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        order_id = store.record_paper_order(
            "2026-06-12",
            _decision(),
            risk_profile="research",
        )
        for _ in range(5):
            store.close_paper_order(order_id, 0.01, max_quantity=1.0)

        reason = store.paper_entry_pause_reason(
            "research",
            bankroll=1000.0,
            target_date="2026-06-13",
            min_resolved_trades=5,
            max_resolved_roi=0.0,
            daily_loss_pct=1.0,
        )

        assert reason is None


def test_live_does_not_pause_from_research_losses():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        for idx in range(5):
            order_id = store.record_paper_order(
                "2026-06-12",
                _decision(f"KXHIGHTSFO-TEST-B{70 + idx}.5"),
                risk_profile="research",
            )
            store.close_paper_order(order_id, 0.01)

        assert store.paper_entry_pause_reason(
            "live",
            bankroll=1000.0,
            target_date="2026-06-13",
        ) is None


def test_live_pauses_on_its_own_bad_resolved_cohort():
    # The trading-intent profile now has its own (looser) breaker: 10 resolved
    # live losers trip it on resolved ROI.
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        for idx in range(10):
            order_id = store.record_paper_order(
                "2026-06-12",
                _decision(f"KXHIGHTSFO-TEST-B{70 + idx}.5"),
                risk_profile="live",
            )
            store.close_paper_order(order_id, 0.01)

        reason = store.paper_entry_pause_reason(
            "live", bankroll=1000.0, target_date="2026-06-13"
        )
        assert reason is not None
        assert "live paused" in reason


def test_resolved_pause_clears_after_the_window_ages_out():
    # The same bad cohort no longer latches the profile off forever: evaluated
    # far enough in the future, the losers fall outside the rolling window.
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        for idx in range(10):
            order_id = store.record_paper_order(
                "2026-06-12",
                _decision(f"KXHIGHTSFO-TEST-B{70 + idx}.5"),
                risk_profile="live",
            )
            store.close_paper_order(order_id, 0.01)

        # Paused now...
        assert store.paper_entry_pause_reason(
            "live", bankroll=1000.0, target_date="2026-06-13"
        ) is not None
        # ...but recovered once the cohort is older than the lookback window.
        later = datetime.now(UTC) + timedelta(days=60)
        assert store.paper_entry_pause_reason(
            "live", bankroll=1000.0, target_date="2026-08-13", now=later
        ) is None


def test_research_pauses_after_daily_loss_limit():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order(
            "2026-06-12",
            _decision(),
            risk_profile="research",
        )
        store.close_paper_order(order_id, 0.01)

        reason = store.paper_entry_pause_reason(
            "research",
            bankroll=1000.0,
            target_date="2026-06-12",
        )

        assert reason is not None
        assert "daily loss" in reason


def test_motion_daily_loss_pause_cannot_pause_target_or_live():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        with store.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO paper_orders (
                    created_at, target_date, market_ticker, label, action,
                    risk_profile, side, contracts, yes_ask, fee_per_contract,
                    cost_per_contract, probability, probability_lcb, edge,
                    edge_lcb, trade_quality_score, expected_profit, status,
                    reasons_json, account_id, research_sleeve,
                    research_policy_version, policy_fingerprint, objective_day,
                    scan_run_id, reentry_fingerprint, closed_at, realized_pnl
                ) VALUES (
                    '2026-07-18T16:00:00+00:00', '2026-07-19',
                    'KXHIGHTSFO-MOTION-LOSS', '80 to 81', 'BUY_NO',
                    'research', 'NO', 1, 0.80, 0.01, 0.81, 0.70, 0.65,
                    0.10, 0.05, 50, 0.10, 'PAPER_CLOSED', '[]', ?, ?, ?, ?,
                    '2026-07-18', 'scan-motion-loss', 'reentry-motion-loss',
                    '2026-07-18T20:00:00+00:00', -50.00
                )
                """,
                (
                    MOTION_POLICY.account_id,
                    MOTION_POLICY.sleeve.value,
                    MOTION_POLICY.policy_version,
                    MOTION_POLICY.policy_fingerprint,
                ),
            )
            assert cursor.lastrowid

        motion_reason = store.paper_entry_pause_reason(
            "research",
            bankroll=MOTION_POLICY.reference_equity,
            target_date="2026-07-19",
            account_id=MOTION_POLICY.account_id,
            daily_loss_pct=MOTION_POLICY.daily_loss_pause_pct,
            now=datetime(2026, 7, 18, 21, 0, tzinfo=UTC),
        )
        target_reason = store.paper_entry_pause_reason(
            "research",
            bankroll=TARGET_POLICY.reference_equity,
            target_date="2026-07-19",
            account_id=TARGET_POLICY.account_id,
            daily_loss_pct=TARGET_POLICY.daily_loss_pause_pct,
            now=datetime(2026, 7, 18, 21, 0, tzinfo=UTC),
        )
        live_reason = store.paper_entry_pause_reason(
            "live",
            bankroll=1000.0,
            target_date="2026-07-19",
            account_id="paper-shared",
            now=datetime(2026, 7, 18, 21, 0, tzinfo=UTC),
        )

        assert motion_reason is not None
        assert "daily loss" in motion_reason
        assert target_reason is None
        assert live_reason is None
