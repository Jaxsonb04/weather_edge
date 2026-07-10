import { describe, expect, it } from "vitest";
import { equitySeries, equitySeriesFromDays, type StrategyLab } from "./strategy";

describe("account equity series", () => {
  it("uses backend closing equity so the visible window retains prior realized P&L", () => {
    const s = {
      daily_summary: {
        starting_bankroll: 1000,
        days: [
          { date: "2026-07-08", cumulative_realized: -38.12, closing_equity: 961.88 },
          { date: "2026-07-09", cumulative_realized: -39.46, closing_equity: 960.54 },
        ],
      },
    } as StrategyLab;

    const series = equitySeries(s);
    expect(series.at(-1)).toEqual({ date: "07-09", equity: 960.54, pnl: -39.46 });
  });

  it("renders profile data as zero-based P&L contribution", () => {
    const series = equitySeriesFromDays(
      [{ date: "2026-07-09", cumulative_realized: -39.46 }],
      0,
    );

    expect(series[0].equity).toBe(-39.46);
  });
});
