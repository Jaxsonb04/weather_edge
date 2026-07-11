import { describe, expect, it } from "vitest";
import {
  cents,
  closedLedger,
  equitySeriesFromDays,
  ledgerByCity,
  money,
  profileGate,
  type ClosedPosition,
  type StrategyLab,
} from "./strategy";

const position = (overrides: Partial<ClosedPosition>): ClosedPosition => ({
  id: 1,
  ticker: "KXHIGHTSFO-26JUL08-B68",
  label: "68° or above",
  side: "yes",
  contracts: 1,
  entry_price: 0.4,
  exit_price: 0.6,
  realized_pnl: 1,
  realized_roi: 0.1,
  quality_score: 70,
  risk_profile: "live",
  target_date: "2026-07-08",
  closed_at: "2026-07-09T10:00:00Z",
  ...overrides,
});

describe("strategy collection helpers", () => {
  it("groups the closed ledger by city and orders by trade count then P&L", () => {
    const rows = [
      position({ id: 1, realized_pnl: 2 }),
      position({ id: 2, realized_pnl: -1 }),
      position({ id: 3, ticker: "KXHIGHTSEA-26JUL08-B70", realized_pnl: 4 }),
      position({ id: 4, ticker: "UNKNOWN", realized_pnl: -2 }),
    ];

    expect(ledgerByCity(rows)).toEqual([
      { slug: "sfo", name: "San Francisco", trades: 2, pnl: 1, wins: 1 },
      { slug: "sea", name: "Seattle", trades: 1, pnl: 4, wins: 1 },
      { slug: "—", name: "Unknown", trades: 1, pnl: -2, wins: 0 },
    ]);
  });

  it("finds one profile gate and tolerates a missing gate collection", () => {
    const gate = { risk_profile: "live", approved: 2, signals: 10 };
    const strategy = {
      daily_summary: { gate_behavior: { by_profile: [gate] } },
    } as StrategyLab;

    expect(profileGate(strategy, "live")).toEqual(gate);
    expect(profileGate({} as StrategyLab, "live")).toBeUndefined();
  });

  it("sorts null closed timestamps after dated rows without mutating input", () => {
    const undated = position({ id: 1, closed_at: null as unknown as string });
    const older = position({ id: 2, closed_at: "2026-07-08T10:00:00Z" });
    const newer = position({ id: 3, closed_at: "2026-07-09T10:00:00Z" });
    const rows = [undated, older, newer];

    expect(closedLedger({ paper_trading: { closed_positions: rows } } as StrategyLab).map((row) => row.id)).toEqual([3, 2, 1]);
    expect(rows.map((row) => row.id)).toEqual([1, 2, 3]);
  });

  it("builds an empty equity series when day rows are missing", () => {
    expect(equitySeriesFromDays(undefined)).toEqual([]);
  });
});

describe("canonical financial formatters", () => {
  it.each([
    [2.5, "+$2.50"],
    [0, "+$0.00"],
    [-2.5, "−$2.50"],
    [Number.NaN, "—"],
    [null, "—"],
  ])("formats money %s as %s", (value, expected) => {
    expect(money(value)).toBe(expected);
  });

  it.each([
    [2.5, "$2.50"],
    [0, "$0.00"],
    [-2.5, "−$2.50"],
  ])("formats unsigned totals %s through the same money helper as %s", (value, expected) => {
    expect(money(value, { sign: "negative-only" })).toBe(expected);
  });

  it.each([
    [2.5, "+$3"],
    [0, "$0"],
    [-2.5, "−$3"],
  ])("formats signed non-zero whole-dollar chart values %s as %s", (value, expected) => {
    expect(money(value, { digits: 0, sign: "except-zero" })).toBe(expected);
  });

  it.each([
    [0.923, "92¢"],
    [-0.125, "−13¢"],
    [null, "—"],
  ])("formats cents %s as %s", (value, expected) => {
    expect(cents(value)).toBe(expected);
  });
});
