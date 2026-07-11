"""The paper monitor's stop-loss veto must only trust fresh model snapshots."""

import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from sfo_kalshi_quant.cities import get_city
from sfo_kalshi_quant.cli import _refresh_same_day_model_reads, main
from sfo_kalshi_quant.config import StrategyConfig
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.exits import ExitSignal
from sfo_kalshi_quant.fees import quadratic_fee_average_per_contract
from sfo_kalshi_quant.kalshi import KalshiUnavailable
from sfo_kalshi_quant.models import (
    BucketProbability,
    EventSnapshot,
    ForecastOutcome,
    ForecastSnapshot,
    IntradaySnapshot,
    MarketBin,
    TradeDecision,
    format_event_date_token,
)
from sfo_kalshi_quant.settlement_day import settlement_clock
from sfo_kalshi_quant.standard_bins import fallback_bins


def _probability(ticker: str, probability: float) -> BucketProbability:
    return BucketProbability(
        ticker=ticker,
        label="77° to 78°",
        probability=probability,
        lower_confidence=probability - 0.1,
        empirical_probability=probability,
        normal_probability=probability,
        effective_n=200,
    )


def _cheap_yes_decision(*, yes_ask: float = 0.08) -> TradeDecision:
    return TradeDecision(
        ticker="KXHIGHTSFO-TEST-B75.5",
        label="74° to 75°",
        action="BUY_YES",
        approved=True,
        probability=0.14,
        probability_lcb=0.05,
        yes_bid=max(0.01, yes_ask - 0.01),
        yes_ask=yes_ask,
        spread=0.01,
        fee_per_contract=0.006,
        cost_per_contract=yes_ask + 0.006,
        edge=0.06,
        edge_lcb=-0.03,
        kelly_fraction=0.01,
        recommended_contracts=5.0,
        expected_profit=0.3,
        reasons=[],
        strike_type="between",
        floor_strike=74.0,
        cap_strike=75.0,
    )


def _stopped_no_decision() -> TradeDecision:
    return TradeDecision(
        ticker="KXHIGHTSFO-TEST-B82.5",
        label="82° or above",
        action="BUY_NO",
        approved=True,
        probability=0.35,
        probability_lcb=0.25,
        yes_bid=0.19,
        yes_ask=0.21,
        spread=0.02,
        fee_per_contract=0.013,
        cost_per_contract=0.803,
        edge=0.05,
        edge_lcb=0.01,
        kelly_fraction=0.01,
        recommended_contracts=3.0,
        expected_profit=0.2,
        reasons=[],
        side="NO",
        entry_bid=0.77,
        entry_ask=0.79,
        strike_type="above",
        floor_strike=82.0,
        cap_strike=None,
    )


def _between_no_decision(
    ticker: str,
    label: str,
    *,
    cost: float,
    floor: float,
    cap: float,
) -> TradeDecision:
    ask = cost - 0.02
    return TradeDecision(
        ticker=ticker,
        label=label,
        action="BUY_NO",
        approved=True,
        probability=0.72,
        probability_lcb=0.60,
        yes_bid=max(0.0, 1.0 - ask - 0.02),
        yes_ask=max(0.0, 1.0 - ask),
        spread=0.02,
        fee_per_contract=0.02,
        cost_per_contract=cost,
        edge=0.10,
        edge_lcb=-0.02,
        kelly_fraction=0.01,
        recommended_contracts=3.0,
        expected_profit=0.30,
        reasons=[],
        side="NO",
        entry_bid=max(0.0, ask - 0.03),
        entry_ask=ask,
        strike_type="between",
        floor_strike=floor,
        cap_strike=cap,
    )


class _FakeKalshiClient:
    yes_bid = 0.03
    yes_ask = 0.05
    no_bid = 0.95
    no_ask = 0.97
    yes_sub_title = "74° to 75°"
    strike_type = "between"
    floor_strike = 74.0
    cap_strike = 75.0

    def get_market(self, ticker: str) -> MarketBin:
        return MarketBin(
            ticker=ticker,
            event_ticker="KXHIGHTSFO-TEST",
            title="Highest temperature in San Francisco?",
            yes_sub_title=self.yes_sub_title,
            strike_type=self.strike_type,
            floor_strike=self.floor_strike,
            cap_strike=self.cap_strike,
            yes_bid=self.yes_bid,
            yes_ask=self.yes_ask,
            no_bid=self.no_bid,
            no_ask=self.no_ask,
            yes_bid_size=10.0,
            yes_ask_size=10.0,
            status="active",
        )


class _FakeYesStopClient(_FakeKalshiClient):
    yes_bid = 0.06
    yes_ask = 0.08
    no_bid = 0.92
    no_ask = 0.94


class _FakeYesSoftStopClient(_FakeKalshiClient):
    yes_bid = 0.075
    yes_ask = 0.09
    no_bid = 0.91
    no_ask = 0.925


class _FakeNoStopClient(_FakeKalshiClient):
    yes_bid = 0.53
    yes_ask = 0.55
    no_bid = 0.45
    no_ask = 0.47
    yes_sub_title = "82° or above"
    strike_type = "above"
    floor_strike = 82.0
    cap_strike = None


class _FakeNoConvergedClient(_FakeKalshiClient):
    """A NO favorite whose price has converged up to the model's fair value."""

    yes_bid = 0.04
    yes_ask = 0.06
    no_bid = 0.94
    no_ask = 0.96
    yes_sub_title = "82° or above"
    strike_type = "above"
    floor_strike = 82.0
    cap_strike = None


def test_monitor_decide_exit_uses_series_and_profile_config_fee_semantics():
    class _BoundaryClient(_FakeKalshiClient):
        yes_bid = 0.50
        yes_ask = 0.52

    captured: dict[str, float] = {}

    def capture_decision(**kwargs):
        captured["net_exit"] = kwargs["net_exit"]
        return ExitSignal("HOLD", "boundary captured")

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        decision = _cheap_yes_decision()
        store.record_paper_order("2026-06-12", decision, risk_profile="live")
        config = StrategyConfig(taker_fee_rate=0.11, fee_multiplier=0.5)

        with (
            patch("sfo_kalshi_quant.cli.KalshiPublicClient", _BoundaryClient),
            patch("sfo_kalshi_quant.cli.strategy_config_for_profile", return_value=config),
            patch("sfo_kalshi_quant.cli.decide_exit", side_effect=capture_decision),
            redirect_stdout(StringIO()),
        ):
            assert main(["--db-path", str(db_path), "--no-color", "paper-monitor"]) == 0

        expected_fee = quadratic_fee_average_per_contract(
            _BoundaryClient.yes_bid,
            decision.recommended_contracts,
            fee_multiplier=config.fee_multiplier,
            taker_rate=config.taker_fee_rate,
            maker_rate=config.maker_fee_rate,
            series_ticker=decision.ticker,
        )

    assert captured["net_exit"] == _BoundaryClient.yes_bid - expected_fee


class _FakeNoBasketClient:
    def get_market(self, ticker: str) -> MarketBin:
        if ticker.endswith("B68.5"):
            no_bid = 0.16
            no_ask = 0.18
            floor = 68.0
            cap = 69.0
            label = "68° to 69°"
        else:
            no_bid = 0.72
            no_ask = 0.74
            floor = 70.0
            cap = 71.0
            label = "70° to 71°"
        return MarketBin(
            ticker=ticker,
            event_ticker="KXHIGHTSFO-TEST",
            title="Highest temperature in San Francisco?",
            yes_sub_title=label,
            strike_type="between",
            floor_strike=floor,
            cap_strike=cap,
            yes_bid=max(0.0, 1.0 - no_ask),
            yes_ask=max(0.0, 1.0 - no_bid),
            no_bid=no_bid,
            no_ask=no_ask,
            yes_bid_size=10.0,
            yes_ask_size=10.0,
            status="active",
        )


def test_monitor_edge_based_take_profit_banks_a_converged_no_favorite():
    """A NO favorite bought at 0.80 whose price has converged to the model's fair
    value (0.93) is banked. The old %-of-cost take-profit targeted 0.80*1.35=1.08,
    which is unreachable, so the favorite silently rode to settlement instead."""
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order("2026-06-12", _stopped_no_decision())
        # Model YES probability 0.07 -> NO fair value 0.93; the live NO bid 0.94
        # nets just above fair value, so the convergence take-profit fires.
        store.record_probabilities(
            "2026-06-12",
            [_probability("KXHIGHTSFO-TEST-B82.5", 0.07)],
        )

        out = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _FakeNoConvergedClient), redirect_stdout(out):
            code = main(["--db-path", str(db_path), "--no-color", "paper-monitor"])

        assert code == 0
        row = store.paper_orders(1)[0]
        assert row["id"] == order_id
        assert row["status"] == "PAPER_CLOSED"
        assert row["realized_pnl"] > 0
        with store.connect() as conn:
            action = conn.execute(
                """
                SELECT action FROM paper_monitor_snapshots
                WHERE order_id = ? ORDER BY id DESC LIMIT 1
                """,
                (order_id,),
            ).fetchone()[0]
        assert action == "CLOSE_TAKE_PROFIT"


def test_monitor_research_no_favorite_rides_converged_profit_to_settlement():
    """Research NO favorites have tiny residual upside and nearly full downside.
    When they are already expensive favorites, do not clip a converged winner;
    leave it open for settlement unless the edge reverses or a real stop fires."""
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order(
            "2026-06-12",
            _stopped_no_decision(),
            risk_profile="research",
        )
        store.record_probabilities(
            "2026-06-12",
            [_probability("KXHIGHTSFO-TEST-B82.5", 0.07)],
        )

        out = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _FakeNoConvergedClient), redirect_stdout(out):
            code = main(["--db-path", str(db_path), "--no-color", "paper-monitor"])

        assert code == 0
        row = store.paper_orders(1)[0]
        assert row["id"] == order_id
        assert row["status"] == "PAPER_FILLED"
        with store.connect() as conn:
            action, reason = conn.execute(
                """
                SELECT action, reason FROM paper_monitor_snapshots
                WHERE order_id = ? ORDER BY id DESC LIMIT 1
                """,
                (order_id,),
            ).fetchone()
        assert action == "HOLD_SETTLEMENT_FIRST"
        assert "settlement-first" in reason


def test_monitor_holds_a_still_mispriced_no_favorite_instead_of_riding_blind():
    """The same favorite, but the price has NOT yet reached fair value (NO bid
    0.84 nets below 0.93): hold for further convergence rather than scalp early."""

    class _BelowFairValueClient(_FakeNoConvergedClient):
        no_bid = 0.84
        no_ask = 0.86

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order("2026-06-12", _stopped_no_decision())
        store.record_probabilities(
            "2026-06-12",
            [_probability("KXHIGHTSFO-TEST-B82.5", 0.07)],
        )

        out = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _BelowFairValueClient), redirect_stdout(out):
            code = main(["--db-path", str(db_path), "--no-color", "paper-monitor"])

        assert code == 0
        row = store.paper_orders(1)[0]
        assert row["status"] == "PAPER_FILLED"  # held open


def test_latest_model_probability_returns_fresh_snapshot():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        store.record_probabilities(
            "2026-06-10", [_probability("KXHIGHTSFO-TEST-B77.5", 0.62)]
        )
        value = store.latest_model_probability("2026-06-10", "KXHIGHTSFO-TEST-B77.5")
        assert value is not None
        assert abs(value - 0.62) < 1e-9


def test_latest_model_probability_ignores_stale_snapshot():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        store.record_probabilities(
            "2026-06-10", [_probability("KXHIGHTSFO-TEST-B77.5", 0.62)]
        )
        stale = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE probability_snapshots SET created_at = ?", (stale,))
        assert store.latest_model_probability("2026-06-10", "KXHIGHTSFO-TEST-B77.5") is None


def test_latest_model_probability_missing_market_is_none():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        assert store.latest_model_probability("2026-06-10", "KXHIGHTSFO-TEST-T78") is None


# --- the veto must read the LIVE decision journal, not the context snapshots ---
# probability_snapshots is written only on the first command per tick
# (--skip-context-snapshots), so it can flatline while decision_snapshots keeps
# flowing -- which silently disabled this veto from 2026-06-16.

def _record_decision_model_read(
    store: PaperStore, target_date: str, ticker: str, side: str, model_probability: float
) -> None:
    decision = TradeDecision(
        ticker=ticker,
        label="82° or above",
        action="BUY_NO" if side == "NO" else "BUY_YES",
        approved=False,
        probability=model_probability,
        probability_lcb=max(0.0, model_probability - 0.05),
        yes_bid=0.19,
        yes_ask=0.21,
        spread=0.02,
        fee_per_contract=0.01,
        cost_per_contract=0.21,
        edge=0.0,
        edge_lcb=0.0,
        kelly_fraction=0.0,
        recommended_contracts=0.0,
        expected_profit=0.0,
        reasons=[],
        side=side,
        model_probability=model_probability,
        strike_type="above",
        floor_strike=82.0,
        cap_strike=None,
    )
    store.record_decisions(target_date, [decision])


def test_latest_model_probability_reads_no_side_decision_journal():
    """decision_snapshots stores model_probability per SIDE; a BUY_NO row's 0.95
    is the NO-side conviction, which must normalize back to YES = 0.05."""
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        _record_decision_model_read(store, "2026-06-18", "KXHIGHTSFO-TEST-B82.5", "NO", 0.95)
        value = store.latest_model_probability("2026-06-18", "KXHIGHTSFO-TEST-B82.5")
        assert value is not None
        assert abs(value - 0.05) < 1e-9


def test_latest_model_probability_prefers_decision_over_probability_snapshot():
    """When both tables have the market, the live decision journal wins."""
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        store.record_probabilities(
            "2026-06-18", [_probability("KXHIGHTSFO-TEST-B82.5", 0.62)]
        )
        _record_decision_model_read(store, "2026-06-18", "KXHIGHTSFO-TEST-B82.5", "YES", 0.10)
        value = store.latest_model_probability("2026-06-18", "KXHIGHTSFO-TEST-B82.5")
        assert value is not None
        assert abs(value - 0.10) < 1e-9


def test_paper_monitor_no_veto_uses_decision_journal_model_read():
    """End-to-end: a NO favorite at its hard floor is HELD by the model veto when
    the only model source is the live decision journal (no probability_snapshots).
    This is the exact path that was dead on 2026-06-18."""
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order("2026-06-12", _stopped_no_decision())
        # NO-side model conviction 0.95 (>= entry cost 0.803) lives ONLY in the
        # decision journal; probability_snapshots is intentionally never written.
        _record_decision_model_read(store, "2026-06-12", "KXHIGHTSFO-TEST-B82.5", "NO", 0.95)

        out = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _FakeNoStopClient), redirect_stdout(out):
            code = main(["--db-path", str(db_path), "--no-color", "paper-monitor"])

        assert code == 0
        assert store.paper_orders(1)[0]["status"] == "PAPER_FILLED"
        with store.connect() as conn:
            action = conn.execute(
                "SELECT action FROM paper_monitor_snapshots WHERE order_id=? ORDER BY id DESC LIMIT 1",
                (order_id,),
            ).fetchone()[0]
        assert action == "HOLD_MODEL_VETO"


def test_paper_monitor_no_stop_held_failsafe_when_no_model_read():
    """The 2026-06-18 regression: NO model source at all (dead context tables).
    The NO favorite at its hard floor must HOLD (fail-safe), not whipsaw out --
    a daily high is monotonic, so the intraday mark is noise we cannot confirm."""
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order("2026-06-12", _stopped_no_decision())
        # Deliberately record NO model read in either table.

        out = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _FakeNoStopClient), redirect_stdout(out):
            code = main(["--db-path", str(db_path), "--no-color", "paper-monitor"])

        assert code == 0
        assert store.paper_orders(1)[0]["status"] == "PAPER_FILLED"
        with store.connect() as conn:
            action = conn.execute(
                "SELECT action FROM paper_monitor_snapshots WHERE order_id=? ORDER BY id DESC LIMIT 1",
                (order_id,),
            ).fetchone()[0]
        assert action == "HOLD_NO_MODEL_READ"


def test_same_day_model_heartbeat_is_safe_off_by_default_after_scan_cutoff():
    target = settlement_clock(city=get_city("sfo")).date()
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        decision = replace(_stopped_no_decision(), ticker="KXHIGHTSFO-HEARTBEAT-T73")
        order_id = store.record_paper_order(target.isoformat(), decision)

        with (
            patch.dict("os.environ", {"PAPER_SAME_DAY_MODEL_HEARTBEAT_ENABLED": "false"}),
            patch("sfo_kalshi_quant.cli.KalshiPublicClient", _FakeNoStopClient),
            redirect_stdout(StringIO()),
        ):
            assert main(["--db-path", str(db_path), "--no-color", "paper-monitor"]) == 0

        with store.connect() as conn:
            action = conn.execute(
                "SELECT action FROM paper_monitor_snapshots WHERE order_id=? ORDER BY id DESC LIMIT 1",
                (order_id,),
            ).fetchone()[0]
            assert conn.execute("SELECT COUNT(*) FROM probability_snapshots").fetchone()[0] == 0
        assert action == "HOLD_NO_MODEL_READ"


def test_same_day_model_heartbeat_does_not_run_before_entry_cutoff():
    city = get_city("sfo")
    before_cutoff = datetime(2026, 7, 10, 13, 59, tzinfo=city.fixed_standard_timezone())
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        rows = [
            {
                "market_ticker": "KXHIGHTSFO-26JUL10-B68.5",
                "target_date": "2026-07-10",
                "risk_profile": "live",
            }
        ]
        with (
            patch("sfo_kalshi_quant.cli.settlement_clock", return_value=before_cutoff),
            patch.object(store, "latest_market_snapshot") as latest_market,
            patch(
                "sfo_kalshi_quant.cli.SfoForecasterAdapter",
                side_effect=AssertionError("heartbeat must stay dormant before cutoff"),
            ),
        ):
            assert _refresh_same_day_model_reads(
                store, rows, forecaster_root=Path(tmp)
            ) == 0
        latest_market.assert_not_called()


def _heartbeat_monitor_fixture(tmp: str, target):
    event_ticker = f"KXHIGHTSFO-{format_event_date_token(target)}"
    markets = fallback_bins(event_ticker, 69.5)
    held_market = markets[-1]
    db_path = Path(tmp) / "paper.db"
    store = PaperStore(db_path)
    decision = replace(
        _stopped_no_decision(),
        ticker=held_market.ticker,
        label=held_market.yes_sub_title,
        strike_type=held_market.strike_type,
        floor_strike=held_market.floor_strike,
        cap_strike=held_market.cap_strike,
    )
    order_id = store.record_paper_order(target.isoformat(), decision)
    store.record_market(
        EventSnapshot(
            event_ticker=event_ticker,
            title="heartbeat input gate test",
            target_date=target,
            markets=markets,
            raw={
                "event_ticker": event_ticker,
                "title": "heartbeat input gate test",
                "markets": [market.raw for market in markets],
            },
        )
    )
    return db_path, store, order_id, held_market


def _heartbeat_adapter(target, *, observed_at: str):
    adapter = Mock()
    adapter.load_calibration_outcomes.return_value = [
        ForecastOutcome(
            local_date=target - timedelta(days=idx + 1),
            predicted_high_f=70.0,
            actual_high_f=70.0 + (idx % 3) - 1.0,
        )
        for idx in range(40)
    ]
    forecast = ForecastSnapshot(
        target_date=target,
        predicted_high_f=70.0,
        station_id="KSFO",
        fetched_at=datetime.now(UTC).isoformat(),
        method="heartbeat-test",
        source_count=4,
        raw={"emos": {"mu": 70.0, "sigma": 1.0}},
    )
    adapter.latest_emos_snapshot.return_value = forecast
    adapter.intraday_snapshot.return_value = IntradaySnapshot(
        target_date=target,
        observed_high_f=70.0,
        latest_temp_f=69.0,
        latest_observed_at=observed_at,
        remaining_forecast_high_f=70.0,
        forecast_fetched_at=datetime.now(UTC).isoformat(),
    )
    adapter.apply_intraday_update.return_value = forecast
    return adapter


def _run_enabled_heartbeat_monitor(db_path: Path, target, adapter) -> None:
    city = get_city("sfo")
    with (
        patch.dict("os.environ", {"PAPER_SAME_DAY_MODEL_HEARTBEAT_ENABLED": "true"}),
        patch(
            "sfo_kalshi_quant.cli.settlement_clock",
            return_value=datetime(
                target.year,
                target.month,
                target.day,
                15,
                tzinfo=city.fixed_standard_timezone(),
            ),
        ),
        patch("sfo_kalshi_quant.cli.SfoForecasterAdapter", return_value=adapter),
        patch("sfo_kalshi_quant.cli.KalshiPublicClient", _FakeNoStopClient),
        redirect_stdout(StringIO()),
    ):
        assert main(["--db-path", str(db_path), "--no-color", "paper-monitor"]) == 0


def test_same_day_model_heartbeat_missing_emos_retains_no_model_read_hold():
    target = settlement_clock(city=get_city("sfo")).date()
    with TemporaryDirectory() as tmp:
        db_path, store, order_id, held_market = _heartbeat_monitor_fixture(tmp, target)
        adapter = _heartbeat_adapter(target, observed_at=datetime.now(UTC).isoformat())
        adapter.latest_emos_snapshot.return_value = None

        _run_enabled_heartbeat_monitor(db_path, target, adapter)

        with store.connect() as conn:
            action = conn.execute(
                "SELECT action FROM paper_monitor_snapshots WHERE order_id=? ORDER BY id DESC LIMIT 1",
                (order_id,),
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT COUNT(*) FROM probability_snapshots WHERE market_ticker=?",
                (held_market.ticker,),
            ).fetchone()[0]
        assert rows == 0
        assert action == "HOLD_NO_MODEL_READ"


def test_same_day_model_heartbeat_stale_observed_high_retains_no_model_read_hold():
    target = settlement_clock(city=get_city("sfo")).date()
    with TemporaryDirectory() as tmp:
        db_path, store, order_id, held_market = _heartbeat_monitor_fixture(tmp, target)
        stale = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
        adapter = _heartbeat_adapter(target, observed_at=stale)

        _run_enabled_heartbeat_monitor(db_path, target, adapter)

        with store.connect() as conn:
            action = conn.execute(
                "SELECT action FROM paper_monitor_snapshots WHERE order_id=? ORDER BY id DESC LIMIT 1",
                (order_id,),
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT COUNT(*) FROM probability_snapshots WHERE market_ticker=?",
                (held_market.ticker,),
            ).fetchone()[0]
        assert rows == 0
        assert action == "HOLD_NO_MODEL_READ"


def test_same_day_model_heartbeat_refreshes_open_position_without_new_entry():
    city = get_city("sfo")
    target = settlement_clock(city=city).date()
    event_ticker = f"KXHIGHTSFO-{format_event_date_token(target)}"
    markets = fallback_bins(event_ticker, 69.5)
    held_market = markets[-1]

    class _HeartbeatAdapter:
        def __init__(self, root, city=None):
            self.city = city or get_city("sfo")

        def load_calibration_outcomes(self, source):
            return [
                ForecastOutcome(
                    local_date=target - timedelta(days=idx + 1),
                    predicted_high_f=70.0,
                    actual_high_f=70.0 + (idx % 3) - 1.0,
                )
                for idx in range(40)
            ]

        def latest_emos_snapshot(self, requested_target):
            return ForecastSnapshot(
                target_date=requested_target,
                predicted_high_f=70.0,
                station_id="KSFO",
                fetched_at=datetime.now(UTC).isoformat(),
                method="heartbeat-test",
                source_count=4,
                raw={"emos": {"mu": 70.0, "sigma": 1.0}},
            )

        def intraday_snapshot(self, requested_target):
            return IntradaySnapshot(
                target_date=requested_target,
                observed_high_f=70.0,
                latest_temp_f=69.0,
                latest_observed_at=datetime.now(UTC).isoformat(),
                remaining_forecast_high_f=70.0,
                forecast_fetched_at=datetime.now(UTC).isoformat(),
            )

        def apply_intraday_update(self, forecast, intraday):
            return forecast

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        decision = replace(
            _stopped_no_decision(),
            ticker=held_market.ticker,
            label=held_market.yes_sub_title,
            strike_type=held_market.strike_type,
            floor_strike=held_market.floor_strike,
            cap_strike=held_market.cap_strike,
        )
        order_id = store.record_paper_order(target.isoformat(), decision)
        event = EventSnapshot(
            event_ticker=event_ticker,
            title="heartbeat test",
            target_date=target,
            markets=markets,
            raw={"event_ticker": event_ticker, "title": "heartbeat test", "markets": [m.raw for m in markets]},
        )
        store.record_market(event)
        distractor_ticker = f"KXHIGHNY-{format_event_date_token(target)}"
        distractor_markets = fallback_bins(distractor_ticker, 89.5)
        store.record_market(
            EventSnapshot(
                event_ticker=distractor_ticker,
                title="later NYC snapshot",
                target_date=target,
                markets=distractor_markets,
                raw={
                    "event_ticker": distractor_ticker,
                    "title": "later NYC snapshot",
                    "markets": [market.raw for market in distractor_markets],
                },
            )
        )
        with store.connect() as conn:
            before_orders = conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0]

        with (
            patch.dict("os.environ", {"PAPER_SAME_DAY_MODEL_HEARTBEAT_ENABLED": "true"}),
            patch(
                "sfo_kalshi_quant.cli.settlement_clock",
                return_value=datetime(
                    target.year,
                    target.month,
                    target.day,
                    15,
                    tzinfo=city.fixed_standard_timezone(),
                ),
            ),
            patch("sfo_kalshi_quant.cli.SfoForecasterAdapter", _HeartbeatAdapter),
            patch("sfo_kalshi_quant.cli.KalshiPublicClient", _FakeNoStopClient),
            redirect_stdout(StringIO()),
        ):
            assert main(["--db-path", str(db_path), "--no-color", "paper-monitor"]) == 0

        with store.connect() as conn:
            action = conn.execute(
                "SELECT action FROM paper_monitor_snapshots WHERE order_id=? ORDER BY id DESC LIMIT 1",
                (order_id,),
            ).fetchone()[0]
            after_orders = conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0]
            heartbeat_rows = conn.execute(
                "SELECT COUNT(*) FROM probability_snapshots WHERE target_date=? AND market_ticker=?",
                (target.isoformat(), held_market.ticker),
            ).fetchone()[0]
        assert action != "HOLD_NO_MODEL_READ"
        assert heartbeat_rows == 1
        assert after_orders == before_orders


def test_paper_monitor_hard_floor_disables_model_veto():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order("2026-06-12", _cheap_yes_decision())
        store.record_probabilities(
            "2026-06-12",
            [_probability("KXHIGHTSFO-TEST-B75.5", 0.14)],
        )

        out = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _FakeKalshiClient), redirect_stdout(out):
            code = main(
                [
                    "--db-path",
                    str(db_path),
                    "--no-color",
                    "paper-monitor",
                    "--model-veto-max-loss-pct",
                    "60",
                ]
            )

        assert code == 0
        row = store.paper_orders(1)[0]
        assert row["id"] == order_id
        assert row["status"] == "PAPER_CLOSED"
        assert row["realized_pnl"] < 0
        with store.connect() as conn:
            action = conn.execute(
                """
                SELECT action
                FROM paper_monitor_snapshots
                WHERE order_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (order_id,),
            ).fetchone()[0]
        assert action == "CLOSE_STOP_LOSS"
        assert "HOLD_MODEL_VETO" not in out.getvalue()


def test_paper_monitor_yes_stop_loss_does_not_model_veto_before_hard_floor():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order(
            "2026-06-12",
            _cheap_yes_decision(yes_ask=0.09),
        )
        store.record_probabilities(
            "2026-06-12",
            [_probability("KXHIGHTSFO-TEST-B75.5", 0.15)],
        )

        out = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _FakeYesStopClient), redirect_stdout(out):
            code = main(
                [
                    "--db-path",
                    str(db_path),
                    "--no-color",
                    "paper-monitor",
                    "--model-veto-max-loss-pct",
                    "60",
                ]
            )

        assert code == 0
        row = store.paper_orders(1)[0]
        assert row["id"] == order_id
        assert row["status"] == "PAPER_CLOSED"
        with store.connect() as conn:
            action = conn.execute(
                """
                SELECT action
                FROM paper_monitor_snapshots
                WHERE order_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (order_id,),
            ).fetchone()[0]
        assert action == "CLOSE_STOP_LOSS"
        assert "HOLD_MODEL_VETO" not in out.getvalue()


def test_paper_monitor_yes_uses_tighter_default_stop_loss():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order(
            "2026-06-12",
            _cheap_yes_decision(yes_ask=0.09),
        )

        out = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _FakeYesSoftStopClient), redirect_stdout(out):
            code = main(["--db-path", str(db_path), "--no-color", "paper-monitor"])

        assert code == 0
        row = store.paper_orders(1)[0]
        assert row["id"] == order_id
        assert row["status"] == "PAPER_CLOSED"
        with store.connect() as conn:
            action, reason = conn.execute(
                """
                SELECT action, reason
                FROM paper_monitor_snapshots
                WHERE order_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (order_id,),
            ).fetchone()
        assert action == "CLOSE_STOP_LOSS"
        assert "25.0%" in reason
        assert "HOLD_MODEL_VETO" not in out.getvalue()


def test_paper_monitor_no_stop_loss_can_model_veto_before_hard_floor():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order("2026-06-12", _stopped_no_decision())
        store.record_probabilities(
            "2026-06-12",
            [_probability("KXHIGHTSFO-TEST-B82.5", 0.10)],
        )

        out = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _FakeNoStopClient), redirect_stdout(out):
            code = main(
                [
                    "--db-path",
                    str(db_path),
                    "--no-color",
                    "paper-monitor",
                    "--model-veto-max-loss-pct",
                    "60",
                ]
            )

        assert code == 0
        row = store.paper_orders(1)[0]
        assert row["id"] == order_id
        assert row["status"] == "PAPER_FILLED"
        with store.connect() as conn:
            action = conn.execute(
                """
                SELECT action
                FROM paper_monitor_snapshots
                WHERE order_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (order_id,),
            ).fetchone()[0]
        assert action == "HOLD_MODEL_VETO"
        assert "above entry cost" in out.getvalue()
        assert "HOLD order" in out.getvalue()


def test_same_day_no_basket_holds_catastrophic_stop_when_model_still_supports_leg():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        stopped_id = store.record_paper_order(
            "2026-06-26",
            _between_no_decision(
                "KXHIGHTSFO-TEST-B68.5",
                "68° to 69°",
                cost=0.62,
                floor=68.0,
                cap=69.0,
            ),
            risk_profile="research",
        )
        store.record_paper_order(
            "2026-06-26",
            _between_no_decision(
                "KXHIGHTSFO-TEST-B70.5",
                "70° to 71°",
                cost=0.84,
                floor=70.0,
                cap=71.0,
            ),
            risk_profile="research",
        )
        _record_decision_model_read(store, "2026-06-26", "KXHIGHTSFO-TEST-B68.5", "NO", 0.66)

        out = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _FakeNoBasketClient), redirect_stdout(out):
            code = main(["--db-path", str(db_path), "--no-color", "paper-monitor"])

        assert code == 0
        assert store.open_paper_order(stopped_id) is not None
        with store.connect() as conn:
            action = conn.execute(
                """
                SELECT action
                FROM paper_monitor_snapshots
                WHERE order_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (stopped_id,),
            ).fetchone()[0]
        assert action == "HOLD_NO_BASKET_VETO"
        assert "same-day NO basket" in out.getvalue()


def test_same_day_no_basket_closes_catastrophic_stop_when_model_thesis_dies():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        stopped_id = store.record_paper_order(
            "2026-06-26",
            _between_no_decision(
                "KXHIGHTSFO-TEST-B68.5",
                "68° to 69°",
                cost=0.62,
                floor=68.0,
                cap=69.0,
            ),
            risk_profile="research",
        )
        store.record_paper_order(
            "2026-06-26",
            _between_no_decision(
                "KXHIGHTSFO-TEST-B70.5",
                "70° to 71°",
                cost=0.84,
                floor=70.0,
                cap=71.0,
            ),
            risk_profile="research",
        )
        _record_decision_model_read(store, "2026-06-26", "KXHIGHTSFO-TEST-B68.5", "NO", 0.40)

        out = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _FakeNoBasketClient), redirect_stdout(out):
            code = main(["--db-path", str(db_path), "--no-color", "paper-monitor"])

        assert code == 0
        assert store.open_paper_order(stopped_id) is None
        row = store.paper_orders(2)[-1]
        assert row["id"] == stopped_id
        assert row["status"] == "PAPER_CLOSED"


def test_paper_monitor_no_veto_requires_model_to_cover_entry_cost():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order("2026-06-12", _stopped_no_decision())
        store.record_probabilities(
            "2026-06-12",
            [_probability("KXHIGHTSFO-TEST-B82.5", 0.35)],
        )

        out = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _FakeNoStopClient), redirect_stdout(out):
            code = main(["--db-path", str(db_path), "--no-color", "paper-monitor"])

        assert code == 0
        row = store.paper_orders(1)[0]
        assert row["id"] == order_id
        assert row["status"] == "PAPER_CLOSED"
        with store.connect() as conn:
            action = conn.execute(
                """
                SELECT action
                FROM paper_monitor_snapshots
                WHERE order_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (order_id,),
            ).fetchone()[0]
        assert action == "CLOSE_STOP_LOSS"
        assert "HOLD_MODEL_VETO" not in out.getvalue()


def test_paper_monitor_default_no_can_veto_past_old_45_pct_floor():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order("2026-06-12", _stopped_no_decision())
        store.record_probabilities(
            "2026-06-12",
            [_probability("KXHIGHTSFO-TEST-B82.5", 0.10)],
        )

        out = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _FakeNoStopClient), redirect_stdout(out):
            code = main(["--db-path", str(db_path), "--no-color", "paper-monitor"])

        assert code == 0
        row = store.paper_orders(1)[0]
        assert row["id"] == order_id
        assert row["status"] == "PAPER_FILLED"
        with store.connect() as conn:
            action = conn.execute(
                """
                SELECT action
                FROM paper_monitor_snapshots
                WHERE order_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (order_id,),
            ).fetchone()[0]
        assert action == "HOLD_MODEL_VETO"
        assert "HOLD order" in out.getvalue()


def test_monitor_surfaces_a_non_network_error_instead_of_silent_hold():
    """A programming/auth error must propagate (loud, non-zero exit), not be
    masked as a benign FETCH_FAILED HOLD that leaves every position unmanaged."""

    class _BrokenClient:
        def get_market(self, ticker):
            raise ValueError("unexpected bug")

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        store.record_paper_order("2026-06-12", _cheap_yes_decision())
        err = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _BrokenClient), redirect_stderr(err):
            code = main(["--db-path", str(db_path), "--no-color", "paper-monitor"])
        assert code == 1  # surfaced, not swallowed
        with store.connect() as conn:
            actions = [r[0] for r in conn.execute("SELECT action FROM paper_monitor_snapshots").fetchall()]
        assert "FETCH_FAILED" not in actions


def test_monitor_holds_open_position_on_transient_kalshi_unavailable():
    """A genuinely transient outage still holds the position as FETCH_FAILED."""

    class _FlakyClient:
        def get_market(self, ticker):
            raise KalshiUnavailable("kalshi down")

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order("2026-06-12", _cheap_yes_decision())
        out = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _FlakyClient), redirect_stdout(out):
            code = main(["--db-path", str(db_path), "--no-color", "paper-monitor"])
        assert code == 0
        assert store.paper_orders(1)[0]["status"] == "PAPER_FILLED"
        with store.connect() as conn:
            action = conn.execute(
                "SELECT action FROM paper_monitor_snapshots WHERE order_id=? ORDER BY id DESC LIMIT 1",
                (order_id,),
            ).fetchone()[0]
        assert action == "FETCH_FAILED"
