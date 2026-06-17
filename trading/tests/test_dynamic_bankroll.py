"""P0-C: dynamic / compounding bankroll -- clamped equity sizing + honest display."""

from pathlib import Path
from tempfile import TemporaryDirectory

from sfo_kalshi_quant.cli import _clamp_sizing_equity
from sfo_kalshi_quant.config import StrategyConfig
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.summary import build_paper_summary


def test_clamp_equity_floors_a_drawdown_and_caps_a_hot_streak():
    start = 1000.0
    # A deep drawdown cannot collapse the sizing base below half the notional.
    assert _clamp_sizing_equity(300.0, start) == 500.0
    # A hot streak cannot balloon it beyond twice the notional.
    assert _clamp_sizing_equity(2500.0, start) == 2000.0
    # Inside the band, equity passes through unchanged (true compounding).
    assert _clamp_sizing_equity(1150.0, start) == 1150.0
    assert _clamp_sizing_equity(850.0, start) == 850.0


def test_paper_summary_reports_current_equity_distinct_from_starting_bankroll():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        forecaster_root = Path(tmp) / "forecaster"
        forecaster_root.mkdir()
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B66.5",
            label="66° to 67°",
            action="BUY_YES",
            approved=True,
            probability=0.40,
            probability_lcb=0.30,
            yes_bid=0.20,
            yes_ask=0.25,
            spread=0.05,
            fee_per_contract=0.01,
            cost_per_contract=0.26,
            edge=0.14,
            edge_lcb=0.05,
            kelly_fraction=0.01,
            recommended_contracts=10.0,
            expected_profit=1.4,
            reasons=[],
            strike_type="between",
            floor_strike=66.0,
            cap_strike=67.0,
        )
        store.record_paper_order("2026-06-03", decision)
        store.settle_paper_orders("2026-06-03", 67)  # 66-67 bracket resolves YES
        realized = store.market_backtest_summary()["realized_pnl"]
        assert realized > 0

        payload = build_paper_summary(
            db_path=db_path,
            forecaster_root=forecaster_root,
            config=StrategyConfig(paper_bankroll=1000.0),
            days=30,
        )
        assert payload["starting_bankroll"] == 1000.0
        # The honest live number moves with realized PnL; static bankroll does not.
        assert round(payload["current_equity"], 2) == round(1000.0 + realized, 2)
        assert payload["current_equity"] != payload["starting_bankroll"]
