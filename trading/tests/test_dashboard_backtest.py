"""P1-E: the published signal backtest scores entry rows, not the decayed last
scan, so approved metrics stop reading as a phantom $0."""

from pathlib import Path
from tempfile import TemporaryDirectory

from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.strategy_research import _signal_backtest_payload

from support import pre_resolution_event


class _StubAdapter:
    def __init__(self, settlements):
        self._settlements = settlements

    def load_cli_settlement_truth(self):
        return self._settlements


def _decision(*, approved, prob, edge, edge_lcb, quality):
    return TradeDecision(
        ticker="KXHIGHTSFO-TEST-B66.5",
        label="66° to 67°",
        action="BUY_YES",
        approved=approved,
        probability=prob,
        probability_lcb=prob - 0.1,
        yes_bid=0.20,
        yes_ask=0.30,
        spread=0.10,
        fee_per_contract=0.01,
        cost_per_contract=0.31,
        edge=edge,
        edge_lcb=edge_lcb,
        kelly_fraction=0.02 if approved else 0.0,
        recommended_contracts=10.0 if approved else 0.0,
        expected_profit=3.1 if approved else 0.0,
        reasons=[] if approved else ["edge decayed"],
        trade_quality_score=quality,
        strike_type="between",
        floor_strike=66.0,
        cap_strike=67.0,
    )


def test_dashboard_backtest_uses_entry_mode_so_approved_signals_are_not_zeroed():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        # The actual entry (approved), then a later decayed scan (rejected) for the
        # same market-side. latest-per-market-side would keep the decayed row and
        # report 0 approved; entry-per-market-side keeps the entry.
        entry = _decision(approved=True, prob=0.70, edge=0.39, edge_lcb=0.29, quality=70.0)
        decayed = _decision(approved=False, prob=0.50, edge=-0.01, edge_lcb=-0.11, quality=20.0)
        store.record_decisions("2026-06-03", [entry], event=pre_resolution_event([entry]))
        store.record_decisions("2026-06-03", [decayed], event=pre_resolution_event([decayed]))

        payload = _signal_backtest_payload(_StubAdapter({"2026-06-03": 67.0}), db_path)

        assert payload["sample_mode"] == "entry-per-market-side"
        assert payload["counts"]["approved_signals"] >= 1
        assert payload["counts"]["settled_signals"] >= 1
