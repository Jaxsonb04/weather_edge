"""Closing-line-value (CLV) and exit-efficiency analysis for the paper journal.

Read-only measurement over ``paper_trading.db``. Phase 0 of the accuracy/trading
uplift work: quantify, before changing any knob, whether early exits are helping
or hurting and how good entry timing is, per order and bucketed by lifecycle /
risk profile / temperature cohort.

Two metrics, deliberately separated by how much they can be trusted:

* **CLV (closing-line value)** -- the position's realizable exit value at the
  last pre-settlement monitor snapshot minus what we paid to enter. Positive CLV
  means the market moved toward the position after entry. It is an entry-quality
  signal that does NOT depend on the single realized settlement outcome, so it is
  the robust headline number. Computed for every order that has monitor
  snapshots; needs no settlement high.

* **Exit drag** -- for orders closed early, realized PnL minus the PnL the same
  position would have booked held to settlement. Negative total exit drag means
  early exits destroyed value. This needs an authoritative settlement high, so it
  is computed ONLY where one is known.

Settlement highs come from the recorded ``settlement_high_f`` (the NWS CLI value
the book actually settled on) propagated across each target date. They are never
reconstructed from ``dataset_station_observations``: the observation high runs a
few degrees below the CLI value (the METAR-vs-CLI discrepancy) which is enough to
flip a bin, so an obs-based counterfactual would silently mislead. Dates without a
recorded high are reported as uncovered rather than guessed.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "paper_trading.db"

# Temperature cohort edges -- mirror config.temperature_cohort so this read-only
# tool has no import-time dependency on strategy config.
_COOL_MAX_F = 69.0
_WARM_MAX_F = 79.0


def temperature_cohort(high_f: float) -> str:
    """Settled-high regime label. Mirrors ``config.temperature_cohort``."""

    if high_f <= _COOL_MAX_F:
        return "cool_le_69f"
    if high_f <= _WARM_MAX_F:
        return "warm_70_79f"
    return "hot_80f_plus"


def bin_resolves_yes(
    strike_type: str | None,
    floor_strike: float | None,
    cap_strike: float | None,
    settlement_high_f: float,
) -> bool:
    """Whether a Kalshi bin resolves YES at a settlement high.

    Mirrors ``models.MarketBin.resolves_yes`` exactly; replicated as a pure
    function so this measurement tool stays decoupled from the market model.
    """

    if strike_type == "less":
        return cap_strike is not None and settlement_high_f < cap_strike
    if strike_type == "greater":
        return floor_strike is not None and settlement_high_f > floor_strike
    return (
        floor_strike is not None
        and cap_strike is not None
        and floor_strike <= settlement_high_f <= cap_strike
    )


def side_won(
    side: str,
    strike_type: str | None,
    floor_strike: float | None,
    cap_strike: float | None,
    settlement_high_f: float,
) -> bool:
    """Whether the order's traded side wins at the settlement high."""

    yes = bin_resolves_yes(strike_type, floor_strike, cap_strike, settlement_high_f)
    return yes if side.upper() == "YES" else not yes


def counterfactual_pnl(contracts: float, cost_per_contract: float, won: bool) -> float:
    """PnL the position would book if held to settlement, after entry cost.

    Mirrors ``backtest_rescore._recorded_pnl``: a winning contract returns
    ``1 - cost`` and a losing one returns ``-cost``.
    """

    return contracts * ((1.0 - cost_per_contract) if won else -cost_per_contract)


def closing_line_value(entry_cost_per_contract: float, closing_mark_per_contract: float) -> float:
    """Per-contract CLV: realizable exit value at the close minus entry cost."""

    return closing_mark_per_contract - entry_cost_per_contract


@dataclass(frozen=True)
class OrderCLV:
    order_id: int
    target_date: str
    status: str
    side: str
    risk_profile: str | None
    contracts: float
    entry_cost: float
    realized_pnl: float | None
    closing_mark: float | None  # last pre-settlement net exit per contract
    settlement_high_f: float | None
    cohort: str | None
    won: bool | None
    counterfactual_hold_pnl: float | None

    @property
    def clv_per_contract(self) -> float | None:
        if self.closing_mark is None:
            return None
        return closing_line_value(self.entry_cost, self.closing_mark)

    @property
    def clv_total(self) -> float | None:
        clv = self.clv_per_contract
        return None if clv is None else clv * self.contracts

    @property
    def exit_drag(self) -> float | None:
        """Realized minus counterfactual-hold PnL (closed orders with a known high)."""

        if self.realized_pnl is None or self.counterfactual_hold_pnl is None:
            return None
        if self.status != "PAPER_CLOSED":
            return None
        return self.realized_pnl - self.counterfactual_hold_pnl


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def bucket_metrics(records: list[OrderCLV]) -> dict[str, object]:
    """Aggregate a list of OrderCLV into a metrics block (mirrors backtest _bucket)."""

    clv = [r.clv_total for r in records if r.clv_total is not None]
    realized = [r.realized_pnl for r in records if r.realized_pnl is not None]
    drag = [r.exit_drag for r in records if r.exit_drag is not None]
    counterfactual = [
        r.counterfactual_hold_pnl for r in records if r.counterfactual_hold_pnl is not None
    ]
    return {
        "orders": len(records),
        "clv_covered": len(clv),
        "clv_total": round(sum(clv), 4) if clv else None,
        "clv_mean_per_order": round(_mean(clv), 4) if clv else None,
        "clv_median_per_order": round(_median(clv), 4) if clv else None,
        "realized_pnl": round(sum(realized), 4) if realized else None,
        "counterfactual_hold_pnl": round(sum(counterfactual), 4) if counterfactual else None,
        "exit_drag_covered": len(drag),
        "exit_drag_total": round(sum(drag), 4) if drag else None,
    }


def _group_by(records: list[OrderCLV], key) -> dict[str, dict[str, object]]:
    groups: dict[str, list[OrderCLV]] = {}
    for record in records:
        groups.setdefault(str(key(record)), []).append(record)
    return {name: bucket_metrics(rows) for name, rows in sorted(groups.items())}


def build_report(records: list[OrderCLV]) -> dict[str, object]:
    """Full CLV report: overall plus by-status / by-profile / by-cohort buckets."""

    covered_dates = sorted({r.target_date for r in records if r.settlement_high_f is not None})
    all_dates = sorted({r.target_date for r in records})
    return {
        "overall": bucket_metrics(records),
        "by_status": _group_by(records, lambda r: r.status),
        "by_risk_profile": _group_by(records, lambda r: r.risk_profile or "unknown"),
        "by_cohort": _group_by(records, lambda r: r.cohort or "unknown"),
        "settlement_coverage": {
            "dates_total": len(all_dates),
            "dates_with_authoritative_high": len(covered_dates),
            "uncovered_dates": [d for d in all_dates if d not in set(covered_dates)],
        },
    }


def _authoritative_highs(conn: sqlite3.Connection) -> dict[str, float]:
    """Per-target-date NWS CLI high from recorded settlements (authoritative only)."""

    rows = conn.execute(
        "SELECT target_date, settlement_high_f FROM paper_orders "
        "WHERE settlement_high_f IS NOT NULL"
    ).fetchall()
    highs: dict[str, float] = {}
    for target_date, high in rows:
        highs[target_date] = float(high)
    return highs


def _closing_marks(conn: sqlite3.Connection) -> dict[int, float]:
    """Last pre-settlement net-exit-per-contract per order from monitor snapshots."""

    rows = conn.execute(
        "SELECT order_id, net_exit_per_contract FROM paper_monitor_snapshots s "
        "WHERE net_exit_per_contract IS NOT NULL AND created_at = ("
        "  SELECT MAX(created_at) FROM paper_monitor_snapshots "
        "  WHERE order_id = s.order_id AND net_exit_per_contract IS NOT NULL)"
    ).fetchall()
    return {int(order_id): float(mark) for order_id, mark in rows}


def load_order_clv(conn: sqlite3.Connection) -> list[OrderCLV]:
    """Assemble OrderCLV records from the paper journal."""

    highs = _authoritative_highs(conn)
    marks = _closing_marks(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, target_date, status, side, risk_profile, contracts, "
        "cost_per_contract, realized_pnl, strike_type, floor_strike, cap_strike "
        "FROM paper_orders"
    ).fetchall()

    records: list[OrderCLV] = []
    for row in rows:
        target_date = row["target_date"]
        high = highs.get(target_date)
        contracts = float(row["contracts"] or 0.0)
        cost = float(row["cost_per_contract"] or 0.0)
        won = None
        counterfactual = None
        cohort = None
        if high is not None:
            won = side_won(
                row["side"], row["strike_type"], row["floor_strike"], row["cap_strike"], high
            )
            counterfactual = counterfactual_pnl(contracts, cost, won)
            cohort = temperature_cohort(high)
        records.append(
            OrderCLV(
                order_id=int(row["id"]),
                target_date=target_date,
                status=row["status"],
                side=row["side"],
                risk_profile=row["risk_profile"],
                contracts=contracts,
                entry_cost=cost,
                realized_pnl=None if row["realized_pnl"] is None else float(row["realized_pnl"]),
                closing_mark=marks.get(int(row["id"])),
                settlement_high_f=high,
                cohort=cohort,
                won=won,
                counterfactual_hold_pnl=counterfactual,
            )
        )
    return records


def _fmt(value: object) -> str:
    if value is None:
        return "   --  "
    if isinstance(value, float):
        return f"{value:+7.3f}"
    return f"{value:>7}"


def _print_bucket_table(title: str, buckets: dict[str, dict[str, object]]) -> None:
    print(f"\n{title}")
    print(f"  {'group':<16} {'n':>4} {'clv_tot':>8} {'clv/ord':>8} {'realized':>9} {'exit_drag':>9}")
    for name, block in buckets.items():
        print(
            f"  {name:<16} {block['orders']:>4} "
            f"{_fmt(block['clv_total'])} {_fmt(block['clv_mean_per_order'])} "
            f"{_fmt(block['realized_pnl'])} {_fmt(block['exit_drag_total'])}"
        )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="paper_trading.db path")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args(argv)

    with sqlite3.connect(args.db) as conn:
        records = load_order_clv(conn)
    report = build_report(records)

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    overall = report["overall"]
    print("=== Closing-Line-Value / Exit-Drag report ===")
    print(f"orders={overall['orders']}  clv_covered={overall['clv_covered']}")
    print(
        f"CLV total={_fmt(overall['clv_total'])}  realized_pnl={_fmt(overall['realized_pnl'])}  "
        f"exit_drag(total, closed w/ known high)={_fmt(overall['exit_drag_total'])} "
        f"over {overall['exit_drag_covered']} orders"
    )
    cov = report["settlement_coverage"]
    print(
        f"settlement coverage: {cov['dates_with_authoritative_high']}/{cov['dates_total']} dates; "
        f"uncovered={cov['uncovered_dates']}"
    )
    _print_bucket_table("By lifecycle status:", report["by_status"])
    _print_bucket_table("By risk profile:", report["by_risk_profile"])
    _print_bucket_table("By settled cohort:", report["by_cohort"])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
