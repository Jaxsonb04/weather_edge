import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import type { StrategyLab } from "../../lib/strategy";

vi.mock("../strategy/EquityCurve", () => ({
  EquityCurve: (props: {
    title?: string;
    eyebrow?: string;
    days?: unknown[];
    startingBankroll?: number;
    contributionMode?: boolean;
  }) => (
    <div
      data-testid="equity-curve"
      data-has-days={props.days ? "yes" : "no"}
      data-starting-bankroll={props.startingBankroll}
      data-contribution-mode={props.contributionMode ? "yes" : "no"}
    >
      {props.eyebrow} — {props.title}
    </div>
  ),
}));

vi.mock("../ui/Reveal", () => ({
  Reveal: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));

import { AnalysisFreshness, LiveStatusStrip, OverviewEquity } from "./StrategyLabView";
import { ReadinessPanel } from "../strategy/ReadinessPanel";

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
    expect(curve).toHaveTextContent("Live Stability · readiness profile — Live Stability — cumulative P&L");
    expect(screen.queryByText(/equity curve unavailable/i)).not.toBeInTheDocument();
  });

  it("prefers the live book's own per-book series over the combined series when both are available", () => {
    render(<OverviewEquity s={withPerBookLiveDays} />);

    const curve = screen.getByTestId("equity-curve");
    expect(curve).toHaveAttribute("data-has-days", "yes");
    expect(curve).toHaveAttribute("data-starting-bankroll", "0");
    expect(curve).toHaveAttribute("data-contribution-mode", "yes");
    expect(screen.queryByText(/equity curve unavailable/i)).not.toBeInTheDocument();
  });
});

describe("AnalysisFreshness", () => {
  it("separates a fresh live publication from older cached historical analysis", () => {
    render(
      <AnalysisFreshness
        s={{
          publication_mode: "fast_public",
          analysis_generated_at: "2026-07-25T08:00:00+00:00",
        } as StrategyLab}
      />,
    );

    expect(screen.getByText(/Historical rescore cached from 2026-07-25 08:00 UTC/i)).toBeInTheDocument();
    expect(screen.getByText(/live paper state and readiness are recomputed/i)).toBeInTheDocument();
  });

  it("states when historical analysis is deferred instead of inventing freshness", () => {
    render(
      <AnalysisFreshness
        s={{
          publication_mode: "fast_public",
          analysis_generated_at: null,
        } as StrategyLab}
      />,
    );

    expect(screen.getByText(/Historical rescore deferred/i)).toBeInTheDocument();
  });
});

describe("LiveStatusStrip accounting gate", () => {
  it("never labels the paper engine live when active ledgers fail reconciliation", () => {
    render(
      <LiveStatusStrip
        s={{
          available: true,
          mode: "paper_research_only",
          accounting: {
            available: false,
            reason: "fresh active paper ledgers invalid or unavailable",
          },
          profiles: [
            { label: "Live Stability", risk_profile: "live", profile_type: "primary" },
            {
              label: "Research ROI",
              risk_profile: "research-target",
              profile_type: "experimental",
            },
          ],
        } as StrategyLab}
      />,
    );

    expect(screen.queryByText("Paper engine live")).not.toBeInTheDocument();
    expect(screen.getByText("Paper account state unavailable")).toBeInTheDocument();
    expect(screen.getByText(/active paper ledgers invalid/i)).toBeInTheDocument();
  });
});

describe("readiness accounting gate", () => {
  it("suppresses both readiness surfaces when accounting is explicitly unavailable", async () => {
    const { ReadinessFinding } = await import("./StrategyLabView");
    const s = {
      available: true,
      mode: "paper_research_only",
      accounting: {
        available: false,
        reason: "fresh active paper ledgers invalid or unavailable",
      },
      real_money_readiness: {
        available: true,
        ready: true,
        verdict: "READY",
        checks_passed: 8,
        checks_total: 8,
        checks: [],
      },
    } as unknown as StrategyLab;

    render(
      <>
        <ReadinessFinding s={s} />
        <ReadinessPanel s={s} />
      </>,
    );

    expect(screen.queryByText(/8\/8 checks passed/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/go-live readiness/i)).not.toBeInTheDocument();
    expect(screen.queryByText("READY")).not.toBeInTheDocument();
  });
});
