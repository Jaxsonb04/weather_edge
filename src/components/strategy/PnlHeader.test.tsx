import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import type { StrategyLab } from "../../lib/strategy";

vi.mock("@heroui/react/card", () => {
  const Card = ({ children }: { children: ReactNode }) => <div>{children}</div>;
  Card.Content = ({ children }: { children: ReactNode }) => <div>{children}</div>;
  return { Card };
});

vi.mock("@heroui-pro/react/kpi", () => {
  const KPI = ({ children }: { children: ReactNode }) => <section>{children}</section>;
  KPI.Header = ({ children }: { children: ReactNode }) => <header>{children}</header>;
  KPI.Title = ({ children }: { children: ReactNode }) => <span>{children}</span>;
  KPI.Content = ({ children }: { children: ReactNode }) => <div>{children}</div>;
  return { KPI };
});

vi.mock("@heroui-pro/react/kpi-group", () => {
  const KPIGroup = ({ children }: { children: ReactNode }) => <div>{children}</div>;
  KPIGroup.Separator = () => <hr />;
  return { KPIGroup };
});

vi.mock("../ui/Reveal", () => ({
  Reveal: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));

import { PnlHeader } from "./PnlHeader";

const baseArtifact = {
  available: true,
  mode: "paper_research_only",
  paper_trading: {
    available: true,
    // All-account aggregate — the backend sums this across every account,
    // live and research alike.
    summary: { realized_pnl: 500, roi: 0.2, hit_rate: 0.6 },
  },
  daily_summary: {
    starting_bankroll: 1000,
    current_equity: 1500,
    totals: { realized_pnl: 120 },
  },
} as StrategyLab;

const withProfiles = (risk_profiles: string[]): StrategyLab =>
  ({
    ...baseArtifact,
    profiles: risk_profiles.map((risk_profile) => ({
      label: risk_profile,
      risk_profile,
      profile_type: risk_profile === "live" ? "primary" : "experimental",
    })),
  }) as StrategyLab;

describe("PnlHeader live-label fallback", () => {
  it("does not render all-account totals under the live-only header when a research book is present", () => {
    render(<PnlHeader s={withProfiles(["live", "research-motion"])} />);

    // The all-account aggregates (contaminated by the research book) must
    // never leak into this live-only header.
    expect(screen.queryByText("$500.00")).not.toBeInTheDocument();
    expect(screen.queryByText("$120.00")).not.toBeInTheDocument();
    expect(screen.queryByText("$1,500.00")).not.toBeInTheDocument();
    // Realized P&L, weekly realized P&L, and realized equity all fall back to
    // an explicit unavailable state instead.
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(3);
  });

  it("still renders the all-account totals when accounting is missing and no research book exists", () => {
    render(<PnlHeader s={withProfiles(["live"])} />);

    expect(screen.getByText("$500.00")).toBeInTheDocument();
    expect(screen.getByText("$120.00")).toBeInTheDocument();
    expect(screen.getByText("$1,500.00")).toBeInTheDocument();
  });

  it("prefers canonical per-book accounting over the all-account totals when both are present", () => {
    const strategy = {
      ...withProfiles(["live", "research-motion"]),
      accounting: {
        available: true,
        initial_capital: 1000,
        all_time_realized_pnl: 42,
        window_realized_pnl: 7,
        realized_equity: 1042,
        cash_balance: 1042,
        reservations: 0,
        available_cash: 1042,
        open_cost_basis: 0,
        unrealized_pnl: null,
        marked_equity: null,
        mark_coverage: "none",
        resolved_capital: 1000,
        return_on_initial_capital: 0.042,
        roi_on_resolved_capital: null,
        reconciliation_status: "ok",
      },
    } as StrategyLab;

    render(<PnlHeader s={strategy} />);

    expect(screen.getByText("$42.00")).toBeInTheDocument();
    expect(screen.getByText("$1,042.00")).toBeInTheDocument();
    expect(screen.queryByText("$500.00")).not.toBeInTheDocument();
  });
});
