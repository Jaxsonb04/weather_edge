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

import { AnalysisFreshness, EvidenceDossier, LiveStatusStrip, OverviewEquity, TrackRecordFinding } from "./StrategyLabView";
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
    expect(screen.getByText(/Readiness equity curve unavailable/i)).toBeInTheDocument();
  });

  it("still plots the combined series under the live label when per-book data is missing but no research book exists", () => {
    render(<OverviewEquity s={degradedLiveOnly} />);

    const curve = screen.getByTestId("equity-curve");
    expect(curve).toHaveAttribute("data-has-days", "no");
    expect(curve).toHaveTextContent("Live Stability · paper readiness profile — Live Stability — cumulative P&L");
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
    expect(screen.getByText(/current paper state is refreshed on every publication/i)).toBeInTheDocument();
    expect(screen.getByText(/readiness evidence is refreshed only by the deploy-time analysis job/i)).toBeInTheDocument();
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

describe("EvidenceDossier", () => {
  it("surfaces the deployed policy version, publication date, and separate account balances", () => {
    render(
      <EvidenceDossier
        s={{
          available: true,
          mode: "paper_research_only",
          live_orders_enabled: false,
          schema_version: 3,
          generated_at: "2026-08-02T10:00:55+00:00",
          profiles: [
            {
              label: "Live Stability",
              risk_profile: "live",
              profile_type: "primary",
              daily_summary: { current_equity: 1025.08 },
              paper_trading: {
                summary: { realized_pnl: 25.08, closed_positions: 37 },
              },
            },
            {
              label: "Research ROI",
              risk_profile: "research-target",
              profile_type: "experimental",
              daily_summary: { current_equity: 983.52 },
              daily_target: {
                available: true,
                policy_version: "research-target-roi-v6",
              },
              paper_trading: {
                summary: { realized_pnl: -16.48, closed_positions: 3 },
              },
            },
            {
              label: "Research ROI v5 (archived 1.5x step)",
              risk_profile: "research-target-v5",
              profile_type: "experimental",
              archived: true,
              paper_trading: {
                summary: { realized_pnl: -4.65, closed_positions: 2 },
              },
            },
          ],
          real_money_readiness: {
            available: true,
            checks_passed: 5,
            checks_total: 12,
          },
        } as unknown as StrategyLab}
      />,
    );

    expect(screen.getAllByText("ROI v6").length).toBeGreaterThan(0);
    expect(screen.getByText("research-target-roi-v6")).toBeInTheDocument();
    expect(screen.getByText("$1,025.08")).toBeInTheDocument();
    expect(screen.getByText("$983.52")).toBeInTheDocument();
    expect(screen.getByText(/Aug 2, 2026.*10:00 UTC/i)).toBeInTheDocument();
    expect(screen.getByText("Paper only")).toBeInTheDocument();
    expect(screen.getByText(/5\/12 checks/i)).toBeInTheDocument();
    expect(screen.getByText(/live orders disabled/i)).toBeInTheDocument();
    expect(screen.getByText(/superseded scale probe · n=2/i)).toBeInTheDocument();
  });

  it("fails closed when active paper accounting is unavailable", () => {
    render(
      <EvidenceDossier
        s={{
          available: true,
          mode: "paper_research_only",
          generated_at: "2026-08-02T10:00:55+00:00",
          accounting: {
            available: false,
            reason: "Fresh active paper ledgers failed reconciliation.",
          },
          profiles: [
            {
              label: "Live Stability",
              risk_profile: "live",
              profile_type: "primary",
              daily_summary: { current_equity: 1999.99 },
            },
            {
              label: "Research ROI",
              risk_profile: "research-target",
              profile_type: "experimental",
              daily_summary: { current_equity: 1888.88 },
              daily_target: { policy_version: "research-target-roi-v99" },
            },
          ],
        } as unknown as StrategyLab}
      />,
    );

    expect(screen.getByRole("alert", { name: /strategy evidence unavailable/i })).toBeInTheDocument();
    expect(screen.getByText(/failed reconciliation/i)).toBeInTheDocument();
    expect(screen.queryByText("$1,999.99")).not.toBeInTheDocument();
    expect(screen.queryByText("$1,888.88")).not.toBeInTheDocument();
    expect(screen.queryByText("ROI v99")).not.toBeInTheDocument();
    expect(screen.queryByText(/two paper ledgers/i)).not.toBeInTheDocument();
  });

  it("fails closed when the publication artifact is unavailable", () => {
    render(
      <EvidenceDossier
        s={{
          available: false,
          mode: "paper_research_only",
          reason: "Live Strategy data belongs on the runtime host after sync.",
          profiles: [
            {
              label: "Stale local profile",
              risk_profile: "live",
              profile_type: "primary",
              daily_summary: { current_equity: 1999.99 },
            },
          ],
        } as unknown as StrategyLab}
      />,
    );

    expect(screen.getByRole("alert", { name: /strategy evidence unavailable/i })).toBeInTheDocument();
    expect(screen.getByText(/belongs on the runtime host/i)).toBeInTheDocument();
    expect(screen.queryByText("$1,999.99")).not.toBeInTheDocument();
    expect(screen.queryByText(/two paper ledgers/i)).not.toBeInTheDocument();
  });

  it("does not promote an archived legacy research profile into the active ledger pair", () => {
    render(
      <EvidenceDossier
        s={{
          available: true,
          mode: "paper_research_only",
          accounting: { available: true },
          profiles: [
            {
              label: "Live Stability",
              risk_profile: "live",
              profile_type: "primary",
              daily_summary: { current_equity: 1025 },
            },
            {
              label: "Archived research",
              risk_profile: "research",
              profile_type: "experimental",
              archived: true,
              daily_summary: { current_equity: 1888.88 },
              daily_target: { policy_version: "research-target-roi-v99" },
            },
          ],
        } as unknown as StrategyLab}
      />,
    );

    expect(screen.getByRole("heading", { name: "Published strategy evidence" })).toBeInTheDocument();
    expect(screen.queryByText(/two paper ledgers/i)).not.toBeInTheDocument();
    expect(screen.queryByText("$1,888.88")).not.toBeInTheDocument();
    expect(screen.queryByText("ROI v99")).not.toBeInTheDocument();
  });
});

describe("TrackRecordFinding", () => {
  it("pairs window statistics with window P&L and labels the total as cross-profile attribution", () => {
    render(
      <TrackRecordFinding
        s={{
          daily_summary: {
            window_days: 7,
            totals: {
              realized_pnl: 58.18,
              cumulative_realized_pnl: 44.2,
              roi: 0.12,
              hit_rate: 0.75,
            },
            side_performance: {
              NO: { trades: 125, realized_pnl: 58.18 },
              YES: { trades: 0, realized_pnl: 0 },
            },
          },
        } as unknown as StrategyLab}
      />,
    );

    expect(screen.getByText("+$58.18")).toBeInTheDocument();
    expect(screen.queryByText("+$44.20")).not.toBeInTheDocument();
    expect(screen.getByText(/cross-profile total is strategy attribution/i)).toBeInTheDocument();
    expect(screen.queryByText(/cross-profile total is research attribution/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/losses are concentrated/i)).not.toBeInTheDocument();
  });
});

describe("LiveStatusStrip accounting gate", () => {
  it("never labels an unavailable publication as a live paper engine", () => {
    render(
      <LiveStatusStrip
        s={{
          available: false,
          mode: "paper_research_only",
          reason: "Strategy publication unavailable after local cleanup.",
        } as StrategyLab}
      />,
    );

    expect(screen.getByRole("alert", { name: /strategy publication unavailable/i })).toBeInTheDocument();
    expect(screen.queryByText(/paper engine live/i)).not.toBeInTheDocument();
  });

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
