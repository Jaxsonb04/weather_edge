from pathlib import Path
from tempfile import TemporaryDirectory

from sfo_kalshi_quant.config import StrategyConfig
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.paper import PaperTrader
from sfo_kalshi_quant.research_shadow import build_research_shadow_report


def _research_explore_decision(
    ticker: str = "KXHIGHTSFO-TEST-B68.5",
    *,
    contracts: float = 10.0,
    cost: float = 0.62,
    side: str = "NO",
) -> TradeDecision:
    ask = cost - 0.02
    return TradeDecision(
        ticker=ticker,
        label="68° to 69°",
        action=f"BUY_{side}",
        approved=True,
        probability=0.78,
        probability_lcb=0.58,
        yes_bid=0.40,
        yes_ask=0.49,
        spread=0.09,
        fee_per_contract=0.02,
        cost_per_contract=cost,
        edge=0.16,
        edge_lcb=-0.04,
        kelly_fraction=0.02,
        recommended_contracts=contracts,
        expected_profit=0.16 * contracts,
        reasons=["portfolio PF-test: sleeve=research_explore, growth=0.001"],
        side=side,
        entry_bid=max(0.0, ask - 0.09),
        entry_ask=ask,
        entry_bid_size=6.0,
        entry_ask_size=10.0,
        trade_quality_score=47.0,
        strike_type="between",
        floor_strike=68.0,
        cap_strike=69.0,
    )


def test_unsampled_research_explore_is_shadow_only_and_not_paper_pnl() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        config = StrategyConfig(research_shadow_sample_probability=0.0)
        trader = PaperTrader(store, config, risk_profile="research")

        order_ids = trader.place_approved(
            "2026-06-26",
            [_research_explore_decision()],
            bankroll=1000.0,
        )

        assert order_ids == []
        shadow_rows = store.research_shadow_orders(10)
        assert len(shadow_rows) == 1
        assert shadow_rows[0]["sampled"] == 0
        assert shadow_rows[0]["sample_probability"] == 0.0
        assert shadow_rows[0]["linked_paper_order_id"] is None
        assert len(store.paper_orders(10)) == 0
        assert store.paper_spend_for_target("2026-06-26", risk_profile="research") == 0.0
        assert store.paper_equity(1000.0, risk_profile="research") == 1000.0
        assert (
            store.paper_entry_pause_reason(
                "research",
                bankroll=1000.0,
                target_date="2026-06-26",
            )
            is None
        )


def test_paused_research_records_shadow_without_paper_order() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        config = StrategyConfig(research_shadow_sample_probability=0.25)
        trader = PaperTrader(store, config, risk_profile="research")
        decision = _research_explore_decision(
            ticker="KXHIGHTSFO-TEST-PAUSED",
            contracts=3.0,
            cost=0.42,
        )
        blocked = decision.__class__(
            **{
                **decision.__dict__,
                "approved": False,
                "signal_approved": True,
                "entry_block_reason": "research paused: daily loss cap reached",
                "reasons": [
                    "research paused: daily loss cap reached",
                    *decision.reasons,
                ],
            }
        )

        shadow_ids = trader.record_research_shadow_candidates(
            "2026-06-26",
            [blocked],
            sample_probability=0.25,
            sampled=False,
        )

        assert len(shadow_ids) == 1
        assert len(store.paper_orders(10)) == 0
        shadow = store.research_shadow_orders(10)[0]
        assert shadow["risk_profile"] == "research"
        assert shadow["sampled"] == 0
        assert shadow["sample_probability"] == 0.25
        assert shadow["linked_paper_order_id"] is None
        assert shadow["contracts"] == 3.0
        assert "research paused: daily loss cap reached" in shadow["reasons_json"]


def test_paused_research_records_positive_lcb_core_shadow() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(store, StrategyConfig(), risk_profile="research")
        decision = _research_explore_decision(
            ticker="KXHIGHTSFO-TEST-CORE",
            contracts=2.0,
            cost=0.44,
        )
        core = decision.__class__(
            **{
                **decision.__dict__,
                "edge_lcb": 0.06,
                "signal_approved": True,
                "entry_block_reason": "research paused: daily loss cap reached",
                "reasons": [
                    "research paused: daily loss cap reached",
                    "portfolio PF-test: sleeve=no_core, growth=0.001000",
                ],
            }
        )

        shadow_ids = trader.record_research_shadow_candidates(
            "2026-06-26",
            [core],
            sampled=False,
        )

        assert len(shadow_ids) == 1
        assert len(store.paper_orders(10)) == 0
        assert store.research_shadow_orders(10)[0]["market_ticker"] == "KXHIGHTSFO-TEST-CORE"


def test_sampled_negative_confidence_research_explore_stays_shadow_only() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        config = StrategyConfig(
            research_shadow_sample_probability=1.0,
            research_shadow_max_contracts=1.0,
            research_shadow_daily_loss_pct=0.0025,
        )
        trader = PaperTrader(store, config, risk_profile="research")

        order_ids = trader.place_approved(
            "2026-06-26",
            [_research_explore_decision(contracts=10.0, cost=0.84)],
            bankroll=1000.0,
        )

        assert order_ids == []
        assert store.paper_orders(10) == []

        shadow_row = store.research_shadow_orders(10)[0]
        assert shadow_row["sampled"] == 1
        assert shadow_row["sample_probability"] == 1.0
        assert shadow_row["linked_paper_order_id"] is None
        assert shadow_row["contracts"] == 10.0


def test_repeated_negative_lcb_research_signals_remain_shadow_only() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        config = StrategyConfig(research_shadow_sample_probability=1.0)
        trader = PaperTrader(store, config, risk_profile="research")
        decision = _research_explore_decision()

        first_ids = trader.place_approved("2026-06-26", [decision], bankroll=1000.0)
        assert first_ids == []

        second_ids = trader.place_approved("2026-06-26", [decision], bankroll=1000.0)

        assert second_ids == []
        assert len(store.paper_orders(10)) == 0
        assert len(store.research_shadow_orders(10)) == 2


def test_research_shadow_report_keeps_ghost_and_sampled_paper_ledgers_separate() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")

        ghost_trader = PaperTrader(
            store,
            StrategyConfig(research_shadow_sample_probability=0.0),
            risk_profile="research",
        )
        assert ghost_trader.place_approved(
            "2026-06-26",
            [_research_explore_decision(contracts=10.0, cost=0.62)],
            bankroll=1000.0,
        ) == []

        sampled_trader = PaperTrader(
            store,
            StrategyConfig(research_shadow_sample_probability=1.0),
            risk_profile="research",
        )
        sampled_ids = sampled_trader.place_approved(
            "2026-06-26",
            [_research_explore_decision(contracts=10.0, cost=0.62)],
            bankroll=1000.0,
        )
        assert sampled_ids == []

        report = build_research_shadow_report(store, settlements={"2026-06-26": 70.0})

        assert report["available"] is True
        assert report["summary"]["shadow_orders"] == 2
        assert report["summary"]["sampled_orders"] == 1
        assert report["paper_executed"]["trades"] == 0
        assert report["paper_executed"]["contracts"] == 0.0
        assert report["shadow_hold_to_settlement"]["trades"] == 2
        assert report["shadow_hold_to_settlement"]["contracts"] == 20.0
        assert report["shadow_hold_to_settlement"]["realized_pnl"] > 0
        assert report["shadow_current_exit_policy"]["trades"] == 0
