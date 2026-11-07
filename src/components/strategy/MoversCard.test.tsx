import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { StrategyLab } from "../../lib/strategy";
import { MoversCard } from "./MoversCard";

describe("MoversCard", () => {
  it("names the window and reads account slugs as account labels", () => {
    const trade = { label: "92° or below", side: "NO", ticker: "KXHIGHTPHX-26SEP27-T93", target_date: "2026-09-27", realized_pnl: 4.72, quality_score: 62 };
    render(
      <MoversCard
        s={{
          daily_summary: {
            window_start: "2026-11-01",
            window_end: "2026-11-07",
            biggest_winners: [{ ...trade, risk_profile: "research-target-v6" }],
            biggest_losers: [{ ...trade, realized_pnl: -3, risk_profile: "live" }],
          },
          profiles: [{ risk_profile: "research-target-v6", archived: true }],
        } as unknown as StrategyLab}
      />,
    );
    expect(screen.getByText("Biggest single trades")).toBeInTheDocument();
    expect(screen.getByText(/P&L realized Nov 1 – Nov 7/)).toBeInTheDocument();
    expect(screen.getByText(/Research ROI v6 · archived/)).toBeInTheDocument();
    expect(screen.getByText(/Live Stability/)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("research-target-v6");
  });
});
