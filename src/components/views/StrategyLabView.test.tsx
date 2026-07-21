import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import type { StrategyLab } from "../../lib/strategy";

vi.mock("../strategy/EquityCurve", () => ({
  EquityCurve: (props: { title?: string; eyebrow?: string; days?: unknown[] }) => (
    <div data-testid="equity-curve" data-has-days={props.days ? "yes" : "no"}>
      {props.eyebrow} — {props.title}
    </div>
  ),
}));

vi.mock("../ui/Reveal", () => ({
  Reveal: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));

import { OverviewEquity } from "./StrategyLabView";

const degradedWithResearchBook = {
  available: true,
  mode: "paper_research_only",
  daily_summary: {
    starting_bankroll: 1000,
    // Combined (all-account) series — includes the research-motion book's
    // activity, so it must never be plotted under the "Live candidate" label.
    days: [{ date: "2026-07-01", cumulative_realized: 999, closing_equity: 1999, realized_pnl: 999 }],
  },
  profiles: [
    { label: "Candidate", risk_profile: "live", profile_type: "primary" },
    { label: "Research motion", risk_profile: "research-motion", profile_type: "experimental" },
  ],
} as StrategyLab;

const degradedLiveOnly = {
  available: true,
  mode: "paper_research_only",
  daily_summary: {
    starting_bankroll: 1000,
    days: [{ date: "2026-07-01", cumulative_realized: 5, closing_equity: 1005, realized_pnl: 5 }],
  },
  profiles: [{ label: "Candidate", risk_profile: "live", profile_type: "primary" }],
} as StrategyLab;

const withPerBookLiveDays = {
  available: true,
  mode: "paper_research_only",
  daily_summary: { starting_bankroll: 1000 },
  profiles: [
    {
      label: "Candidate",
      risk_profile: "live",
      profile_type: "primary",
      daily_summary: {
        days: [{ date: "2026-07-01", cumulative_realized: 5, closing_equity: 1005, realized_pnl: 5 }],
        window_days: 7,
      },
    },
    { label: "Research motion", risk_profile: "research-motion", profile_type: "experimental" },
  ],
} as StrategyLab;

describe("OverviewEquity live-curve fallback", () => {
  it("does not plot the combined series under the live label when per-book data is missing and a research book exists", () => {
    render(<OverviewEquity s={degradedWithResearchBook} />);

    expect(screen.queryByTestId("equity-curve")).not.toBeInTheDocument();
    expect(screen.getByText(/Live candidate equity curve unavailable/i)).toBeInTheDocument();
  });

  it("still plots the combined series under the live label when per-book data is missing but no research book exists", () => {
    render(<OverviewEquity s={degradedLiveOnly} />);

    const curve = screen.getByTestId("equity-curve");
    expect(curve).toHaveAttribute("data-has-days", "no");
    expect(curve).toHaveTextContent("Live candidate · real-money profile — Live candidate — cumulative P&L");
    expect(screen.queryByText(/equity curve unavailable/i)).not.toBeInTheDocument();
  });

  it("prefers the live book's own per-book series over the combined series when both are available", () => {
    render(<OverviewEquity s={withPerBookLiveDays} />);

    const curve = screen.getByTestId("equity-curve");
    expect(curve).toHaveAttribute("data-has-days", "yes");
    expect(screen.queryByText(/equity curve unavailable/i)).not.toBeInTheDocument();
  });
});
