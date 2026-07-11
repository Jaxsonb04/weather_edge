import sqlite3
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from sfo_kalshi_quant import archive, db, strategy_research
from sfo_kalshi_quant.forecast import SfoForecasterAdapter
from sfo_kalshi_quant.models import MarketBin
from sfo_kalshi_quant.settlement_truth import (
    load_cli_settlement_truth,
    normalize_settlement_truth,
    settlement_for_market,
)


def test_date_only_legacy_truth_can_only_settle_sfo() -> None:
    truth = normalize_settlement_truth({"2026-07-08": 67.0})

    assert settlement_for_market(truth, "KXHIGHTSFO-26JUL08-B67", "2026-07-08") == 67.0
    assert settlement_for_market(truth, "KXHIGHNY-26JUL08-B87", "2026-07-08") is None


def test_legacy_cli_schema_fails_closed_for_settlement_sensitive_loaders() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "weather.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE cli_settlements (station_id TEXT, local_date TEXT, "
                "max_temperature_f REAL)"
            )
            conn.executemany(
                "INSERT INTO cli_settlements VALUES (?, ?, ?)",
                [
                    ("KSFO", "2026-07-08", 67.0),
                    ("KNYC", "2026-07-08", 89.0),
                ],
            )
            conn.execute(
                "CREATE TABLE nws_daily_high_ground_truth "
                "(station_id TEXT, local_date TEXT, high_f REAL, is_complete INTEGER)"
            )
            conn.executemany(
                "INSERT INTO nws_daily_high_ground_truth VALUES (?, ?, ?, 1)",
                [
                    ("KSFO", "2026-07-08", 62.0),
                    ("KNYC", "2026-07-08", 95.0),
                ],
            )

        adapter = SfoForecasterAdapter(Path(tmp))
        assert adapter.load_cli_settlement_highs() == {}
        assert adapter.load_cli_settlement_truth() == {}
        assert adapter.load_ksfo_daily_highs() == {}
        with sqlite3.connect(db_path) as conn:
            assert load_cli_settlement_truth(conn) == {}


def test_adapter_excludes_preliminary_cli_truth_when_finality_column_exists() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "weather.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE cli_settlements (station_id TEXT, local_date TEXT, "
                "max_temperature_f REAL, is_final INTEGER NOT NULL DEFAULT 1)"
            )
            conn.executemany(
                "INSERT INTO cli_settlements VALUES (?, ?, ?, ?)",
                [
                    ("KSFO", "2026-07-08", 68.0, 0),
                    ("KSFO", "2026-07-07", 71.0, 1),
                ],
            )

        adapter = SfoForecasterAdapter(Path(tmp))

        assert adapter.load_cli_settlement_highs() == {date(2026, 7, 7): 71.0}
        assert adapter.load_cli_settlement_truth() == {
            ("KXHIGHTSFO", "2026-07-07"): 71.0
        }


def _resolution_row(
    *,
    strike_type: str | None,
    floor_strike: float | None,
    cap_strike: float | None,
    label: str,
) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE bins (strike_type TEXT, floor_strike REAL, "
        "cap_strike REAL, label TEXT)"
    )
    conn.execute(
        "INSERT INTO bins VALUES (?, ?, ?, ?)",
        (strike_type, floor_strike, cap_strike, label),
    )
    return conn.execute("SELECT * FROM bins").fetchone()


def _market_bin(
    *,
    strike_type: str,
    floor_strike: float | None,
    cap_strike: float | None,
) -> MarketBin:
    return MarketBin(
        ticker="TEST",
        event_ticker="TEST-EVENT",
        title="",
        yes_sub_title="",
        strike_type=strike_type,
        floor_strike=floor_strike,
        cap_strike=cap_strike,
        yes_bid=0.0,
        yes_ask=1.0,
        no_bid=0.0,
        no_ask=1.0,
        yes_bid_size=0.0,
        yes_ask_size=0.0,
        status="settled",
    )


def test_unknown_strike_with_bounds_uses_canonical_between_semantics_everywhere() -> None:
    """Guard the verifier-proven divergence before consolidating the four copies."""

    row = _resolution_row(
        strike_type="future_range_name",
        floor_strike=70.0,
        cap_strike=72.0,
        # The legacy label fallback deliberately disagrees with the typed bounds.
        label="80 or above",
    )
    expected = _market_bin(
        strike_type="future_range_name",
        floor_strike=70.0,
        cap_strike=72.0,
    ).resolves_yes(71.0)

    assert expected is True
    assert db._row_resolves_yes(row, 71.0) is expected
    assert db._decision_row_resolves_yes(row, 71.0) is expected
    assert archive._resolves_yes("future_range_name", 70.0, 72.0, 71.0) is expected


def test_all_bin_resolution_adapters_match_market_bin_boundaries() -> None:
    cases = [
        ("less", None, 70.0, 69.0, True),
        ("less", None, 70.0, 70.0, False),
        ("greater", 72.0, None, 72.0, False),
        ("greater", 72.0, None, 73.0, True),
        ("between", 70.0, 72.0, 70.0, True),
        ("between", 70.0, 72.0, 72.0, True),
        ("between", 70.0, 72.0, 73.0, False),
    ]

    for strike, floor, cap, high, expected in cases:
        row = _resolution_row(
            strike_type=strike,
            floor_strike=floor,
            cap_strike=cap,
            label="intentionally irrelevant",
        )
        market = _market_bin(
            strike_type=strike,
            floor_strike=floor,
            cap_strike=cap,
        )
        assert market.resolves_yes(high) is expected
        assert db._row_resolves_yes(row, high) is expected
        assert db._decision_row_resolves_yes(row, high) is expected
        assert archive._resolves_yes(strike, floor, cap, high) is expected


def test_legacy_row_without_typed_strikes_keeps_label_fallback() -> None:
    below = _resolution_row(
        strike_type=None,
        floor_strike=None,
        cap_strike=None,
        label="69° or below",
    )
    above = _resolution_row(
        strike_type=None,
        floor_strike=None,
        cap_strike=None,
        label="74° or above",
    )

    assert db._row_resolves_yes(below, 69.0) is True
    assert db._row_resolves_yes(below, 70.0) is False
    assert db._row_resolves_yes(above, 74.0) is True
    assert db._row_resolves_yes(above, 73.0) is False


def test_pre_resolution_rule_has_one_shared_home() -> None:
    assert db._is_pre_resolution_decision is strategy_research._is_strategy_pre_resolution
