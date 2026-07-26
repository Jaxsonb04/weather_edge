import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { StrategyLab } from "../../lib/strategy";
import { ArchivedPerformance } from "./ArchivedPerformance";

describe("ArchivedPerformance", () => {
  it("shows prior books as read-only achievements without blending their balances", () => {
    const s = {
      profiles: [
        {
          label: "Live Stability",
          risk_profile: "live",
          profile_type: "primary",
        },
        {
          label: "Legacy live achieved performance",
          risk_profile: "live-legacy",
          profile_type: "primary",
          archived: true,
          account_key: "legacy_live",
          paper_trading: {
            summary: {
              closed_positions: 51,
              win_count: 43,
              loss_count: 8,
              realized_pnl: 45.7,
            },
          },
        },
        {
          label: "Research motion (archived)",
          risk_profile: "research-motion",
          profile_type: "experimental",
          archived: true,
          account_key: "research_motion",
          paper_trading: {
            summary: {
              closed_positions: 458,
              win_count: 320,
              loss_count: 138,
              realized_pnl: -1.96,
              open_positions: 26,
            },
          },
        },
      ],
      accounting: {
        archived_accounts: [
          {
            key: "legacy_live",
            profile_key: "legacy-shared-account",
            label: "Legacy shared paper account",
            account_id: "legacy",
            role: "legacy_shared",
            status: "ARCHIVED",
            initial_equity: 1000,
            realized_equity: 958.38,
            realized_pnl: -41.62,
            cash_balance: 958.38,
            available_cash: 958.38,
            reservations: 0,
            open_cost_basis: 0,
            unrealized_pnl: null,
            marked_equity: null,
            mark_coverage: "complete_no_open_positions",
            reconciliation_status: "reconciled",
          },
          {
            key: "research_motion",
            profile_key: "research-motion",
            label: "Research motion",
            account_id: "motion",
            role: "research_motion_archived",
            status: "ARCHIVED_SETTLING",
            initial_equity: 1000,
            realized_equity: 998.04,
            realized_pnl: -1.96,
            cash_balance: 974.78,
            available_cash: 974.78,
            reservations: 0,
            open_cost_basis: 23.26,
            open_positions: 26,
            pending_limit_orders: 1,
            pending_limit_risk: 0.8,
            unrealized_pnl: 0.19,
            marked_equity: 998.23,
            mark_coverage: "complete",
            reconciliation_status: "reconciled",
          },
        ],
      },
    } as unknown as StrategyLab;

    render(<ArchivedPerformance s={s} />);

    expect(screen.getByText("Legacy live achieved performance")).toBeInTheDocument();
    expect(screen.getByText("Research motion (archived)")).toBeInTheDocument();
    expect(screen.getByText("Strategy attribution")).toBeInTheDocument();
    expect(screen.getByText("Historical account balances")).toBeInTheDocument();
    expect(screen.getByText("Legacy shared paper account")).toBeInTheDocument();
    expect(
      screen.getByText(/early live and research orders used one economic paper account/i),
    ).toBeInTheDocument();
    expect(screen.getByText("$958.38")).toBeInTheDocument();
    expect(screen.getByText("$998.04")).toBeInTheDocument();
    expect(
      screen.getByText(
        /26 open positions and 1 pending limit order still settling/i,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/read-only/i)).toBeInTheDocument();
    expect(screen.queryByText("Live Stability")).not.toBeInTheDocument();
  });
});
