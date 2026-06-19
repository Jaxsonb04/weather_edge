"""The 4->2 profile collapse must preserve history: legacy names alias to the
survivors on read, and a one-time DB migration rewrites stored strings so raw
SQL filters keep matching the accumulated AWS paper books."""

from pathlib import Path
from tempfile import TemporaryDirectory

from sfo_kalshi_quant.config import (
    StrategyConfig,
    normalize_risk_profile_name as N,
    strategy_config_for_profile,
)
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import TradeDecision


def test_research_forecast_age_is_not_stricter_than_base():
    # The loosest collector must not silently inherit live's tightened 12h
    # freshness gate via the **LIVE_PROFILE_OVERRIDES spread.
    base = StrategyConfig()
    research = strategy_config_for_profile("research")
    assert research.max_forecast_age_hours >= base.max_forecast_age_hours


def test_live_volatility_caps_are_raised_and_ordered():
    # The volatility retune raised the binding caps; they must stay ordered
    # (position <= event <= daily) and above the strict base so Kelly can flow.
    base = StrategyConfig()
    live = strategy_config_for_profile("live")
    assert live.max_position_risk_pct > base.max_position_risk_pct
    assert (
        live.max_position_risk_pct
        <= live.max_event_risk_pct
        <= live.max_target_exposure_pct
    )


def _decision(ticker: str) -> TradeDecision:
    return TradeDecision(
        ticker=ticker,
        label="70° to 71°",
        action="BUY_NO",
        approved=True,
        probability=0.90,
        probability_lcb=0.85,
        yes_bid=0.08,
        yes_ask=0.10,
        spread=0.02,
        fee_per_contract=0.01,
        cost_per_contract=0.91,
        edge=0.04,
        edge_lcb=0.02,
        kelly_fraction=0.01,
        recommended_contracts=2.0,
        expected_profit=0.08,
        reasons=[],
        trade_quality_score=80.0,
        side="NO",
        strike_type="between",
        floor_strike=70.0,
        cap_strike=71.0,
    )


def test_normalize_aliases_legacy_names():
    cases = [
        ("", "live"),
        ("live", "live"),
        ("balanced", "live"),
        ("conservative", "live"),
        ("real", "live"),
        ("research", "research"),
        ("exploratory", "research"),
        ("fast-feedback", "research"),
        ("fast", "research"),
        ("collector", "research"),
        ("FAST_FEEDBACK", "research"),  # case + underscore normalization
        ("  Balanced  ", "live"),
    ]
    for name, expected in cases:
        assert N(name) == expected, f"{name!r} -> {N(name)!r}, expected {expected!r}"


def test_normalize_rejects_unknown_profile():
    try:
        N("definitely-not-a-profile")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an unknown profile name")


def test_legacy_stored_profile_names_are_migrated_on_init():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        # Record valid rows, then stamp legacy names directly to simulate a DB
        # written before the collapse.
        store.record_paper_order("2026-06-12", _decision("T-A"), risk_profile="live")
        store.record_paper_order("2026-06-12", _decision("T-B"), risk_profile="research")
        store.record_decisions("2026-06-12", [_decision("T-C")], risk_profile="live")
        with store.connect() as conn:
            conn.execute("UPDATE paper_orders SET risk_profile='balanced' WHERE market_ticker='T-A'")
            conn.execute("UPDATE paper_orders SET risk_profile='fast-feedback' WHERE market_ticker='T-B'")
            conn.execute(
                "UPDATE decision_snapshots SET risk_profile='exploratory' WHERE market_ticker='T-C'"
            )

        # Re-opening the store runs init() -> the one-time migration.
        PaperStore(db_path)

        with store.connect() as conn:
            orders = dict(
                conn.execute("SELECT market_ticker, risk_profile FROM paper_orders").fetchall()
            )
            snaps = dict(
                conn.execute(
                    "SELECT market_ticker, risk_profile FROM decision_snapshots"
                ).fetchall()
            )
        assert orders["T-A"] == "live"  # balanced -> live
        assert orders["T-B"] == "research"  # fast-feedback -> research
        assert snaps["T-C"] == "research"  # exploratory -> research


def test_migrated_rows_are_found_by_new_name_filters():
    # The whole point of the migration: a query for the new name finds the
    # historical row that was stored under the legacy name.
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        store.record_paper_order("2026-06-12", _decision("T-OLD"), risk_profile="live")
        with store.connect() as conn:
            conn.execute("UPDATE paper_orders SET risk_profile='balanced' WHERE market_ticker='T-OLD'")

        PaperStore(db_path)  # migrate

        # paper_spend_for_target filters COALESCE(risk_profile,'live')='live'.
        spend = store.paper_spend_for_target("2026-06-12", risk_profile="live")
        assert spend > 0.0
