from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sfo_kalshi_quant.arbitrage import build_arbitrage_opportunities
from sfo_kalshi_quant.cli import main
from sfo_kalshi_quant.cities import get_city
from sfo_kalshi_quant.config import StrategyConfig
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import MarketBin


def _market(
    label: str,
    *,
    ticker: str,
    strike_type: str,
    floor: float | None = None,
    cap: float | None = None,
    yes_ask: float,
    no_ask: float,
    yes_bid: float = 0.01,
    no_bid: float = 0.01,
    yes_size: float = 100.0,
    no_size: float = 100.0,
    status: str = "active",
) -> MarketBin:
    return MarketBin(
        ticker=ticker,
        event_ticker="KXHIGHTSFO-26JUN12",
        title=f"SFO high {label}",
        yes_sub_title=label,
        strike_type=strike_type,
        floor_strike=floor,
        cap_strike=cap,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        yes_bid_size=no_size,
        yes_ask_size=yes_size,
        status=status,
    )


def _config() -> StrategyConfig:
    return StrategyConfig(
        max_event_risk_pct=0.50,
        max_contracts_per_market=100.0,
    )


def _complete_ladder(*, yes_asks: tuple[float, float, float], no_asks: tuple[float, float, float]):
    return [
        _market(
            "65° or below",
            ticker="KXHIGHTSFO-26JUN12-T66",
            strike_type="less",
            cap=66,
            yes_ask=yes_asks[0],
            no_ask=no_asks[0],
        ),
        _market(
            "66° to 67°",
            ticker="KXHIGHTSFO-26JUN12-B66.5",
            strike_type="between",
            floor=66,
            cap=67,
            yes_ask=yes_asks[1],
            no_ask=no_asks[1],
        ),
        _market(
            "68° or above",
            ticker="KXHIGHTSFO-26JUN12-T67",
            strike_type="greater",
            floor=67,
            yes_ask=yes_asks[2],
            no_ask=no_asks[2],
        ),
    ]


def test_same_bin_box_arbitrage_buys_yes_and_no_when_combined_cost_is_below_payout() -> None:
    market = _market(
        "66° to 67°",
        ticker="KXHIGHTSFO-26JUN12-B66.5",
        strike_type="between",
        floor=66,
        cap=67,
        yes_ask=0.45,
        no_ask=0.48,
        yes_size=20.0,
        no_size=30.0,
    )

    opportunities = build_arbitrage_opportunities(
        [market],
        config=_config(),
        bankroll=100.0,
    )

    box = next(row for row in opportunities if row.kind == "BOX_YES_NO")
    assert box.approved, box.reasons
    assert box.contracts == 20.0
    assert [leg.side for leg in box.legs] == ["YES", "NO"]
    assert [leg.decision.side for leg in box.legs] == ["YES", "NO"]
    assert box.guaranteed_payout == 20.0
    assert box.guaranteed_profit > 0.0
    assert box.total_spend < box.guaranteed_payout


def test_full_ladder_yes_arbitrage_covers_every_active_temperature_bin() -> None:
    markets = _complete_ladder(
        yes_asks=(0.20, 0.30, 0.40),
        no_asks=(0.80, 0.70, 0.60),
    )

    opportunities = build_arbitrage_opportunities(
        markets,
        config=_config(),
        bankroll=100.0,
    )

    full_yes = next(row for row in opportunities if row.kind == "FULL_LADDER_YES")
    assert full_yes.approved, full_yes.reasons
    assert full_yes.contracts > 0.0
    assert [leg.market.ticker for leg in full_yes.legs] == [market.ticker for market in markets]
    assert {leg.side for leg in full_yes.legs} == {"YES"}
    assert full_yes.guaranteed_payout == full_yes.contracts
    assert full_yes.total_spend < full_yes.guaranteed_payout


def test_full_ladder_no_arbitrage_pays_for_all_but_the_resolving_bin() -> None:
    markets = _complete_ladder(
        yes_asks=(0.40, 0.30, 0.20),
        no_asks=(0.50, 0.55, 0.60),
    )

    opportunities = build_arbitrage_opportunities(
        markets,
        config=_config(),
        bankroll=100.0,
    )

    full_no = next(row for row in opportunities if row.kind == "FULL_LADDER_NO")
    assert full_no.approved, full_no.reasons
    assert len(full_no.legs) == 3
    assert {leg.side for leg in full_no.legs} == {"NO"}
    assert full_no.guaranteed_payout == full_no.contracts * 2
    assert full_no.total_spend < full_no.guaranteed_payout


def test_full_ladder_arbitrage_rejects_incomplete_active_temperature_coverage() -> None:
    markets = [
        _market(
            "66° to 67°",
            ticker="KXHIGHTSFO-26JUN12-B66.5",
            strike_type="between",
            floor=66,
            cap=67,
            yes_ask=0.20,
            no_ask=0.80,
        ),
        _market(
            "68° or above",
            ticker="KXHIGHTSFO-26JUN12-T67",
            strike_type="greater",
            floor=67,
            yes_ask=0.20,
            no_ask=0.80,
        ),
    ]

    opportunities = build_arbitrage_opportunities(
        markets,
        config=_config(),
        bankroll=100.0,
    )

    full_ladders = [row for row in opportunities if row.kind.startswith("FULL_LADDER")]
    assert full_ladders
    assert not any(row.approved for row in full_ladders)
    assert all(any("coverage" in reason for reason in row.reasons) for row in full_ladders)


def test_arbitrage_sizing_respects_whole_contract_budget_after_group_fees() -> None:
    market = _market(
        "66° to 67°",
        ticker="KXHIGHTSFO-26JUN12-B66.5",
        strike_type="between",
        floor=66,
        cap=67,
        yes_ask=0.45,
        no_ask=0.48,
        yes_size=100.0,
        no_size=100.0,
    )

    opportunities = build_arbitrage_opportunities(
        [market],
        config=_config(),
        bankroll=100.0,
        max_spend=5.0,
    )

    box = next(row for row in opportunities if row.kind == "BOX_YES_NO")
    assert box.approved, box.reasons
    assert box.contracts == 5.0
    assert box.total_spend <= 5.0
    assert box.with_contracts(6.0).total_spend > 5.0


def test_arbitrage_cli_scans_offline_event_ladder_without_forecast_inputs() -> None:
    with TemporaryDirectory() as tmp:
        event_path = Path(tmp) / "event.json"
        _write_event_fixture(event_path, "KXHIGHTSFO-26JUN13")

        out = io.StringIO()
        with redirect_stdout(out):
            code = main(
                [
                    "--no-color",
                    "arbitrage",
                    "--target-date",
                    "2026-06-13",
                    "--offline-events",
                    str(event_path),
                    "--max-arb-spend",
                    "10",
                ]
            )

        text = out.getvalue()
        assert code == 0
        assert "arbitrage scan" in text
        assert "FULL_LADDER_YES" in text
        assert "BOX_YES_NO" in text
        assert "forecast" not in text.lower()


def test_arbitrage_cli_can_place_grouped_paper_orders_from_offline_event() -> None:
    with TemporaryDirectory() as tmp:
        event_path = Path(tmp) / "event.json"
        db_path = Path(tmp) / "paper.db"
        _write_event_fixture(event_path, "KXHIGHTSFO-26JUN13")

        out = io.StringIO()
        with redirect_stdout(out):
            code = main(
                [
                    "--db-path",
                    str(db_path),
                    "--bankroll",
                    "1000",
                    "--no-color",
                    "arbitrage",
                    "--target-date",
                    "2026-06-13",
                    "--offline-events",
                    str(event_path),
                    "--max-arb-spend",
                    "5",
                    "--place-paper",
                ]
            )

        assert code == 0
        assert "recorded paper arbitrage orders" in out.getvalue()
        rows = PaperStore(db_path).paper_orders(20)
        assert len(rows) >= 2
        assert all(str(row["action"]).startswith("ARBITRAGE_BUY_") for row in rows)


def test_nyc_arbitrage_entry_gate_blocks_at_20z_fixed_est_cutoff() -> None:
    city = get_city("nyc")
    instant = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)

    def fixed_clock(now=None, selected_city=None):
        selected = selected_city or get_city("sfo")
        return instant.astimezone(selected.fixed_standard_timezone())

    with TemporaryDirectory() as tmp:
        event_path = Path(tmp) / "event.json"
        db_path = Path(tmp) / "paper.db"
        _write_event_fixture(event_path, "KXHIGHNY-26JUL10")

        with (
            patch("sfo_kalshi_quant.cli.settlement_clock", side_effect=fixed_clock),
            redirect_stdout(io.StringIO()),
        ):
            code = main(
                [
                    "--db-path",
                    str(db_path),
                    "--bankroll",
                    "1000",
                    "--no-color",
                    "arbitrage",
                    "--city",
                    city.slug,
                    "--target-date",
                    "2026-07-10",
                    "--offline-events",
                    str(event_path),
                    "--max-arb-spend",
                    "5",
                    "--place-paper",
                ]
            )

        assert code == 0
        assert PaperStore(db_path).paper_orders(20) == []


def _write_event_fixture(path: Path, event_ticker: str) -> None:
    path.write_text(
        json.dumps(
            {
                "event": {
                    "event_ticker": event_ticker,
                    "title": "SFO high temperature test event",
                    "markets": [
                        _market_payload(
                            event_ticker,
                            "T66",
                            "65° or below",
                            "less",
                            yes_ask=0.20,
                            no_ask=0.50,
                            cap=66,
                        ),
                        _market_payload(
                            event_ticker,
                            "B66.5",
                            "66° to 67°",
                            "between",
                            yes_ask=0.30,
                            no_ask=0.55,
                            floor=66,
                            cap=67,
                        ),
                        _market_payload(
                            event_ticker,
                            "T67",
                            "68° or above",
                            "greater",
                            yes_ask=0.40,
                            no_ask=0.60,
                            floor=67,
                        ),
                    ],
                }
            }
        ),
        encoding="utf-8",
    )


def _market_payload(
    event_ticker: str,
    suffix: str,
    label: str,
    strike_type: str,
    *,
    yes_ask: float,
    no_ask: float,
    floor: float | None = None,
    cap: float | None = None,
) -> dict[str, object]:
    return {
        "ticker": f"{event_ticker}-{suffix}",
        "event_ticker": event_ticker,
        "title": f"SFO high {label}",
        "yes_sub_title": label,
        "strike_type": strike_type,
        "floor_strike": "" if floor is None else floor,
        "cap_strike": "" if cap is None else cap,
        "yes_bid_dollars": f"{max(0.01, yes_ask - 0.01):.4f}",
        "yes_ask_dollars": f"{yes_ask:.4f}",
        "no_bid_dollars": f"{max(0.01, no_ask - 0.01):.4f}",
        "no_ask_dollars": f"{no_ask:.4f}",
        "yes_bid_size_fp": 100,
        "yes_ask_size_fp": 100,
        "status": "active",
    }
