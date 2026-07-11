"""Tests for the config-parameterized re-scoring backtest.

The cornerstone is ROUND-TRIP FIDELITY: reconstructing a recorded decision
snapshot and re-running TradeEvaluator under the SAME config must reproduce the
original decision (approval, probability, edge, cost, size). That proves the
snapshot -> MarketBin/BucketProbability reconstruction is faithful, so a
DIFFERENT config genuinely measures the counterfactual rather than a
reconstruction artifact.
"""

from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from sfo_kalshi_quant.backtest_rescore import (
    reconstruct_market,
    reconstruct_probability,
    run_rescore,
)
from sfo_kalshi_quant.cli import main
from sfo_kalshi_quant.config import StrategyConfig
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import BucketProbability, MarketBin
from sfo_kalshi_quant.risk import TradeEvaluator

from support import pre_resolution_event


def _no_favorite_market() -> MarketBin:
    # A NO favorite: YES is cheap (0.14/0.16), so NO is 0.84/0.86. The bin is
    # 66-67; a high of 70 resolves NO (in the money for the NO side).
    return MarketBin(
        ticker="KXHIGHTSFO-TEST-B66.5",
        event_ticker="KXHIGHTSFO-TEST",
        title="",
        yes_sub_title="66 to 67",
        strike_type="between",
        floor_strike=66.0,
        cap_strike=67.0,
        yes_bid=0.14,
        yes_ask=0.16,
        no_bid=0.84,
        no_ask=0.86,
        yes_bid_size=50.0,
        yes_ask_size=50.0,
        status="active",
        raw={"no_bid_size_fp": 50.0, "no_ask_size_fp": 50.0},
    )


def _no_favorite_probability() -> BucketProbability:
    # YES point 0.06 (-> NO 0.94), tight band, so the NO side clears its cost.
    return BucketProbability(
        ticker="KXHIGHTSFO-TEST-B66.5",
        label="66 to 67",
        probability=0.06,
        lower_confidence=0.04,
        empirical_probability=0.06,
        normal_probability=0.06,
        effective_n=80.0,
        residual_probability=0.06,
        ensemble_probability=0.06,
        model_probability=0.06,
        market_probability=0.12,
        intraday_probability=0.06,
    )


def _record_and_read(store: PaperStore, target_date: str, decision):
    store.record_decisions(
        target_date, [decision], event=pre_resolution_event([decision])
    )
    rows = store.sampled_decision_rows(sample_mode="entry-per-market-side")
    assert len(rows) == 1
    return rows[0]


def test_rescore_roundtrip_reproduces_no_side_decision():
    config = StrategyConfig()
    market = _no_favorite_market()
    probability = _no_favorite_probability()
    original = TradeEvaluator(config).evaluate_market(
        market, probability, bankroll=1000.0, side="NO"
    )
    assert original.approved
    assert original.recommended_contracts > 0

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        row = _record_and_read(store, "2026-06-03", original)

        # Reconstruct + re-run the SAME config -> must reproduce the decision.
        rebuilt_market = reconstruct_market(row)
        rebuilt_prob = reconstruct_probability(row)
        replayed = TradeEvaluator(config).evaluate_market(
            rebuilt_market, rebuilt_prob, bankroll=1000.0, side="NO"
        )

    assert replayed.approved == original.approved
    assert replayed.side == "NO"
    assert abs(replayed.probability - original.probability) < 1e-9
    assert abs(replayed.probability_lcb - original.probability_lcb) < 1e-9
    assert abs(replayed.cost_per_contract - original.cost_per_contract) < 1e-9
    assert abs(replayed.edge - original.edge) < 1e-9
    assert abs(replayed.edge_lcb - original.edge_lcb) < 1e-9
    assert replayed.recommended_contracts == original.recommended_contracts


def test_entry_sampling_normalizes_legacy_null_side_from_action():
    config = StrategyConfig()
    no_decision = TradeEvaluator(config).evaluate_market(
        _no_favorite_market(), _no_favorite_probability(), bankroll=1000.0, side="NO"
    )
    yes_decision = replace(no_decision, action="BUY_YES", side="YES")

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        store.record_decisions(
            "2026-06-03",
            [yes_decision, no_decision],
            event=pre_resolution_event([yes_decision, no_decision]),
        )
        with store.connect() as conn:
            conn.execute("CREATE TABLE legacy_decisions AS SELECT * FROM decision_snapshots")
            conn.execute("DROP TABLE decision_snapshots")
            conn.execute("ALTER TABLE legacy_decisions RENAME TO decision_snapshots")
            conn.execute("UPDATE decision_snapshots SET side = NULL")

        rows = store.sampled_decision_rows(sample_mode="entry-per-market-side")

    assert {row["action"] for row in rows} == {"BUY_YES", "BUY_NO"}


def test_entry_sampling_normalizes_legacy_empty_side_from_action():
    config = StrategyConfig()
    no_decision = TradeEvaluator(config).evaluate_market(
        _no_favorite_market(), _no_favorite_probability(), bankroll=1000.0, side="NO"
    )
    yes_decision = replace(no_decision, action="BUY_YES", side="YES")

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        store.record_decisions(
            "2026-06-03",
            [yes_decision, no_decision],
            event=pre_resolution_event([yes_decision, no_decision]),
        )
        with store.connect() as conn:
            conn.execute("UPDATE decision_snapshots SET side = ''")

        rows = store.sampled_decision_rows(sample_mode="entry-per-market-side")

    assert {row["action"] for row in rows} == {"BUY_YES", "BUY_NO"}


def test_rescore_roundtrip_reproduces_yes_side_decision():
    config = StrategyConfig()
    # A YES favorite: cheap NO, YES ~0.86.
    market = replace(
        _no_favorite_market(),
        yes_bid=0.84,
        yes_ask=0.86,
        no_bid=0.14,
        no_ask=0.16,
    )
    probability = replace(_no_favorite_probability(), probability=0.94, lower_confidence=0.92,
                          model_probability=0.94, market_probability=0.88,
                          residual_probability=0.94, ensemble_probability=0.94,
                          intraday_probability=0.94)
    original = TradeEvaluator(config).evaluate_market(
        market, probability, bankroll=1000.0, side="YES"
    )
    assert original.approved

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        row = _record_and_read(store, "2026-06-03", original)
        replayed = TradeEvaluator(config).evaluate_market(
            reconstruct_market(row), reconstruct_probability(row), bankroll=1000.0, side="YES"
        )

    assert replayed.approved
    assert abs(replayed.probability - original.probability) < 1e-9
    assert abs(replayed.probability_lcb - original.probability_lcb) < 1e-9
    assert abs(replayed.cost_per_contract - original.cost_per_contract) < 1e-9
    assert replayed.recommended_contracts == original.recommended_contracts


def test_run_rescore_settles_winning_no_favorite_by_day():
    config = StrategyConfig()
    decision = TradeEvaluator(config).evaluate_market(
        _no_favorite_market(), _no_favorite_probability(), bankroll=1000.0, side="NO"
    )
    assert decision.approved

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        store.record_decisions(
            "2026-06-03", [decision], event=pre_resolution_event([decision])
        )
        rows = store.sampled_decision_rows(sample_mode="entry-per-market-side")
        # High of 70 is outside 66-67 -> resolves NO -> the NO position WINS.
        result = run_rescore(rows, {"2026-06-03": 70.0}, config, bankroll=1000.0)

    counts = result["counts"]
    assert counts["approved_under_candidate_config"] == 1
    assert counts["settled_decisions"] == 1
    assert counts["independent_days"] == 1
    cand = result["candidate"]
    assert cand["wins"] == 1
    assert cand["realized_pnl"] > 0
    assert cand["hit_rate_per_trade"] == 1.0
    assert cand["ending_equity"] > result["starting_bankroll"]
    assert result["by_side"]["NO"]["trades"] == 1


def test_run_rescore_floors_fractional_high_to_integer_settlement():
    # Kalshi settles off the INTEGER NWS daily climate value. A fractional
    # ground-truth high of 67.4 rounds to the official integer 67, which IS in
    # the 66-67 bin (resolves YES) -> the NO favorite LOSES. Without integer
    # flooring, resolves_yes(67.4) would be False and the NO position would
    # wrongly be scored a win.
    config = StrategyConfig()
    decision = TradeEvaluator(config).evaluate_market(
        _no_favorite_market(), _no_favorite_probability(), bankroll=1000.0, side="NO"
    )
    assert decision.approved
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        store.record_decisions(
            "2026-06-03", [decision], event=pre_resolution_event([decision])
        )
        rows = store.sampled_decision_rows(sample_mode="entry-per-market-side")
        result = run_rescore(rows, {"2026-06-03": 67.4}, config, bankroll=1000.0)
    assert result["counts"]["settled_decisions"] == 1
    cand = result["candidate"]
    assert cand["wins"] == 0
    assert cand["realized_pnl"] < 0


def test_run_rescore_keeps_two_city_settlements_separate_on_the_same_date():
    config = StrategyConfig()
    sfo_decision = TradeEvaluator(config).evaluate_market(
        _no_favorite_market(), _no_favorite_probability(), bankroll=1000.0, side="NO"
    )
    ny_market = replace(
        _no_favorite_market(),
        ticker="KXHIGHNY-TEST-B66.5",
        event_ticker="KXHIGHNY-TEST",
    )
    ny_probability = replace(_no_favorite_probability(), ticker=ny_market.ticker)
    ny_decision = TradeEvaluator(config).evaluate_market(
        ny_market, ny_probability, bankroll=1000.0, side="NO"
    )
    assert sfo_decision.approved and ny_decision.approved

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        store.record_decisions(
            "2026-06-03", [sfo_decision, ny_decision],
            event=pre_resolution_event([sfo_decision, ny_decision]),
        )
        rows = store.sampled_decision_rows(sample_mode="entry-per-market-side")
        result = run_rescore(
            rows,
            {
                ("KXHIGHTSFO", "2026-06-03"): 70.0,
                ("KXHIGHNY", "2026-06-03"): 67.0,
            },
            config,
            bankroll=1000.0,
        )

    assert result["counts"]["settled_decisions"] == 2
    assert result["candidate"]["wins"] == 1
    assert result["candidate"]["losses"] == 1
    assert result["evidence_kind"] == "snapshot_rescore"
    assert result["promotion_eligible"] is False


def test_run_rescore_reports_portfolio_drawdown_and_sleeve_attribution():
    config = StrategyConfig()
    base = TradeEvaluator(config).evaluate_market(
        _no_favorite_market(), _no_favorite_probability(), bankroll=1000.0, side="NO"
    )
    assert base.approved
    no_core = replace(
        base,
        reasons=[*base.reasons, "portfolio PF-TEST: sleeve=no_core, growth=0.001000"],
    )
    arb_leg = replace(
        base,
        action="ARBITRAGE_BUY_NO",
        reasons=[*base.reasons, "portfolio PF-TEST: sleeve=arbitrage, growth=0.000100"],
    )

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        store.record_decisions(
            "2026-06-03", [no_core], event=pre_resolution_event([no_core])
        )
        store.record_decisions(
            "2026-06-04", [arb_leg], event=pre_resolution_event([arb_leg])
        )
        rows = store.sampled_decision_rows(sample_mode="entry-per-market-side")
        result = run_rescore(
            rows,
            {"2026-06-03": 70.0, "2026-06-04": 67.4},
            config,
            bankroll=1000.0,
        )

    portfolio = result["portfolio"]
    assert portfolio["independent_days"] == 2
    assert portfolio["hit_rate_per_day"] == 0.5
    assert portfolio["max_drawdown"] > 0
    assert portfolio["max_drawdown_pct"] > 0
    assert portfolio["by_sleeve"]["no_core"]["trades"] == 1
    assert portfolio["by_sleeve"]["arbitrage"]["trades"] == 1
    assert portfolio["by_side"]["NO"]["trades"] == 2


def test_run_rescore_counterfactual_rejects_under_tighter_config():
    # The recorded entry approves under conservative; a config whose minimum
    # posterior exceeds the trade's probability rejects it. That proves the
    # rescore re-decides under the candidate config rather than replaying the
    # recorded approval.
    base = StrategyConfig()
    decision = TradeEvaluator(base).evaluate_market(
        _no_favorite_market(), _no_favorite_probability(), bankroll=1000.0, side="NO"
    )
    assert decision.approved

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        store.record_decisions(
            "2026-06-03", [decision], event=pre_resolution_event([decision])
        )
        rows = store.sampled_decision_rows(sample_mode="entry-per-market-side")

        loose = run_rescore(rows, {"2026-06-03": 70.0}, base, bankroll=1000.0)
        # min_edge way above any achievable edge -> rejects every row.
        tight = run_rescore(
            rows, {"2026-06-03": 70.0}, replace(base, min_edge=0.95), bankroll=1000.0
        )

    assert loose["counts"]["approved_under_candidate_config"] == 1
    assert tight["counts"]["approved_under_candidate_config"] == 0
    assert tight["counts"]["settled_decisions"] == 0
    # The recorded config still shows 1 approval on the same set either way.
    assert tight["counts"]["approved_under_recorded_config"] == 1


def test_backtest_rescore_cli_dispatches_and_exits_clean():
    config = StrategyConfig()
    decision = TradeEvaluator(config).evaluate_market(
        _no_favorite_market(), _no_favorite_probability(), bankroll=1000.0, side="NO"
    )
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        root.mkdir()
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        store.record_decisions(
            "2026-06-03", [decision], event=pre_resolution_event([decision])
        )
        with redirect_stdout(StringIO()) as out:
            rc = main(
                [
                    "--no-color",
                    "--db-path",
                    str(db_path),
                    "--forecaster-root",
                    str(root),
                    "--risk-profile",
                    "balanced",
                    "backtest-rescore",
                ]
            )
    assert rc == 0
    # No weather.db -> no settlements, but the candidate decision is still
    # re-scored and surfaced as approved-without-settlement.
    text = out.getvalue()
    assert "config rescore" in text
    assert "approved_under_candidate_config: 1" in text
