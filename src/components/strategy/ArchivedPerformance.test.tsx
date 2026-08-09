import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { StrategyLab } from "../../lib/strategy";
import { ArchivedPerformance } from "./ArchivedPerformance";

vi.mock("./EquityCurve", () => ({
  EquityCurve: (props: {
    title?: string;
    description?: string;
    days?: unknown[];
    startingBankroll?: number;
    contributionMode?: boolean;
  }) => (
    <div
      data-testid="archive-curve"
      data-days={props.days?.length ?? 0}
      data-starting-bankroll={props.startingBankroll}
      data-contribution-mode={props.contributionMode ? "yes" : "no"}
    >
      {props.title} · {props.description}
    </div>
  ),
}));

const days = (pnl: number) => [
  {
    date: "2026-07-27",
    cumulative_realized: 0,
    realized_pnl: 0,
    wins: 0,
    losses: 0,
    resolved: 0,
  },
  {
    date: "2026-08-02",
    cumulative_realized: pnl,
    realized_pnl: pnl,
    wins: pnl >= 0 ? 1 : 0,
    losses: pnl < 0 ? 1 : 0,
    resolved: 1,
  },
];

const profile = (
  riskProfile: string,
  label: string,
  closed: number,
  wins: number,
  losses: number,
  pnl: number,
  extra: Record<string, unknown> = {},
) => ({
  label,
  risk_profile: riskProfile,
  profile_type: riskProfile === "live-legacy" ? "primary" : "experimental",
  archived: true,
  paper_trading: {
    summary: {
      closed_positions: closed,
      win_count: wins,
      loss_count: losses,
      realized_pnl: pnl,
      hit_rate: closed ? wins / closed : null,
      roi: closed ? pnl / 100 : null,
      open_positions: 0,
      pending_limit_orders: 0,
      ...extra,
    },
  },
  daily_summary: {
    window_days: 7,
    window_start: "2026-07-27",
    window_end: "2026-08-02",
    days: days(pnl),
  },
});

describe("ArchivedPerformance", () => {
  it("shows only the evidence-bearing lineage with a dated attribution curve per era", () => {
    const s = {
      profiles: [
        { label: "Live Stability", risk_profile: "live", profile_type: "primary" },
        profile("live-legacy", "Legacy live achieved performance", 80, 62, 18, 45.7),
        profile("research-target-v1", "Research target v1 (archived control)", 76, 65, 11, 53.46),
        profile("research-target-v2", "Research target v2 (archived $16 experiment)", 0, 0, 0, 0),
        profile("research-target-v3", "Research ROI v3 (archived oversize experiment)", 35, 29, 6, 5.44),
        profile(
          "research-target-v4",
          "Research ROI v4 (archived breadth restoration)",
          14,
          14,
          0,
          41.29,
          { open_positions: 1 },
        ),
        profile("research-target-v5", "Research ROI v5 (archived 1.5x step)", 2, 0, 2, -4.65),
        profile("research-motion", "Research motion (archived execution learning)", 512, 262, 250, -0.08),
        profile("research", "Legacy research (archived)", 154, 77, 77, -107.53),
      ],
      accounting: {
        archived_accounts: [
          {
            key: "legacy_live",
            profile_key: "legacy-shared-account",
            attribution_profile_key: "live-legacy",
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
            key: "research_target_v1",
            profile_key: "research-target-v1",
            label: "Research target v1",
            account_id: "target-v1",
            role: "research_target_archived",
            status: "ARCHIVED",
            initial_equity: 1000,
            realized_equity: 1053.46,
            realized_pnl: 53.46,
            cash_balance: 1053.46,
            available_cash: 1053.46,
            reservations: 0,
            open_cost_basis: 0,
            unrealized_pnl: null,
            marked_equity: null,
            mark_coverage: "complete_no_open_positions",
            reconciliation_status: "reconciled",
          },
        ],
      },
    } as unknown as StrategyLab;

    const { container } = render(<ArchivedPerformance s={s} />);

    expect(screen.getByText("Research decision trail")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Prior readiness benchmark/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Baseline control record/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Rejected ROI policy/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Adopted ROI revision/i })).toHaveAttribute("aria-selected", "true");
    expect(screen.getAllByRole("tab")).toHaveLength(4);
    expect(screen.getAllByTestId("archive-curve")).toHaveLength(1);
    expect(screen.getByTestId("archive-curve")).toHaveAttribute("data-days", "2");
    expect(screen.getByTestId("archive-curve")).toHaveAttribute("data-starting-bankroll", "0");
    expect(screen.getByTestId("archive-curve")).toHaveAttribute("data-contribution-mode", "yes");
    expect(screen.getByTestId("archive-curve")).toHaveTextContent(/Adopted ROI revision/i);
    expect(screen.getByTestId("archive-curve")).toHaveTextContent(/Jul 27.*Aug 2, 2026/i);
    expect(screen.getByTestId("archive-curve")).toHaveTextContent(/not account equity/i);

    expect(container.querySelectorAll("[data-archive-option]")).toHaveLength(4);
    expect(container.querySelectorAll("[data-archive-profile]")).toHaveLength(1);
    expect(screen.queryByText(/Research target v2/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Research ROI v5/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Research motion/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Legacy research \(archived\)/i)).not.toBeInTheDocument();
    expect(screen.getByText("4 featured eras")).toBeInTheDocument();
    expect(screen.getByText("8 frozen profiles retained")).toBeInTheDocument();
    expect(screen.getByText("80 resolved")).toBeInTheDocument();
    expect(screen.getByText("Inspect Adopted ROI revision daily evidence")).toBeInTheDocument();
    expect(screen.getAllByRole("region", { name: /scrollable daily evidence table/i })).toHaveLength(1);
    expect(screen.getByRole("region", { name: /scrollable daily evidence table/i })).toHaveAttribute("tabindex", "0");

    fireEvent.click(screen.getByRole("tab", { name: /Prior readiness benchmark/i }));
    expect(screen.getByRole("tab", { name: /Prior readiness benchmark/i })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("archive-curve")).toHaveTextContent(/Prior readiness benchmark/i);
    expect(screen.getByText("Inspect Prior readiness benchmark daily evidence")).toBeInTheDocument();
    expect(container.querySelector("[data-archive-profile=\"live-legacy\"]")).toBeInTheDocument();
  });

  it("keeps the sparkline extremes inside the viewBox instead of clipping them", () => {
    const s = {
      profiles: [
        profile("research-target-v4", "Research ROI v4 (archived breadth restoration)", 14, 14, 0, 41.29),
      ],
    } as unknown as StrategyLab;

    render(<ArchivedPerformance s={s} />);

    const points = document
      .querySelector("[data-archive-option] polyline")!
      .getAttribute("points")!
      .split(" ")
      .map((pair) => Number(pair.split(",")[1]));

    // The 2px stroke is centred on the path and does not scale, so a value
    // mapped flush to the 0/18 viewBox edges loses its outer half.
    expect(points.length).toBeGreaterThan(1);
    expect(Math.min(...points)).toBeGreaterThan(0);
    expect(Math.max(...points)).toBeLessThan(18);
  });

  it("supports arrow-key navigation across the compact era tabs", () => {
    const s = {
      profiles: [
        profile("live-legacy", "Legacy live achieved performance", 80, 62, 18, 45.7),
        profile("research-target-v1", "Research target v1", 76, 65, 11, 53.46),
        profile("research-target-v3", "Research target v3", 35, 29, 6, 5.44),
        profile("research-target-v4", "Research target v4", 15, 15, 0, 42.37),
      ],
    } as unknown as StrategyLab;

    render(<ArchivedPerformance s={s} />);
    const adopted = screen.getByRole("tab", { name: /Adopted ROI revision/i });
    fireEvent.keyDown(adopted, { key: "ArrowRight" });
    const prior = screen.getByRole("tab", { name: /Prior readiness benchmark/i });
    expect(prior).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(prior, { key: "ArrowLeft" });
    expect(adopted).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(adopted, { key: "Home" });
    expect(prior).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(prior, { key: "End" });
    expect(adopted).toHaveAttribute("aria-selected", "true");
  });

  it("labels a published archive window with zero activity without inventing evidence", () => {
    const quiet = profile(
      "live-legacy",
      "Legacy live achieved performance",
      80,
      62,
      18,
      45.7,
    );
    quiet.daily_summary.days = [
      {
        date: "2026-08-02",
        cumulative_realized: 45.7,
        realized_pnl: 0,
        wins: 0,
        losses: 0,
        resolved: 0,
      },
    ];
    const s = { profiles: [quiet] } as unknown as StrategyLab;

    render(<ArchivedPerformance s={s} />);

    expect(screen.getByText(/No activity recorded across this published run/i)).toBeInTheDocument();
    expect(screen.getByTestId("archive-curve")).toHaveAttribute("data-days", "1");
  });

  it("keeps strategy attribution separate from exact economic account balances", () => {
    const s = {
      profiles: [
        profile("live-legacy", "Legacy live achieved performance", 80, 62, 18, 45.7),
        profile("research-target-v1", "Research target v1 (archived control)", 76, 65, 11, 53.46),
      ],
      accounting: {
        archived_accounts: [
          {
            profile_key: "legacy-shared-account",
            attribution_profile_key: "live-legacy",
            account_id: "legacy",
            role: "legacy_shared",
            initial_equity: 1000,
            realized_equity: 958.38,
            realized_pnl: -41.62,
            cash_balance: 958.38,
            available_cash: 958.38,
            reservations: 0,
            open_cost_basis: 0,
            unrealized_pnl: null,
            marked_equity: null,
            mark_coverage: "complete",
            reconciliation_status: "reconciled",
          },
          {
            profile_key: "research-target-v1",
            account_id: "target-v1",
            role: "research_target_archived",
            initial_equity: 1000,
            realized_equity: 1053.46,
            realized_pnl: 53.46,
            cash_balance: 1053.46,
            available_cash: 1053.46,
            reservations: 0,
            open_cost_basis: 0,
            unrealized_pnl: null,
            marked_equity: null,
            mark_coverage: "complete",
            reconciliation_status: "reconciled",
          },
        ],
      },
    } as unknown as StrategyLab;

    render(<ArchivedPerformance s={s} />);

    fireEvent.click(screen.getByRole("tab", { name: /Prior readiness benchmark/i }));
    expect(screen.getByText(/shared historical ledger: no strategy-specific balance/i)).toBeInTheDocument();
    expect(screen.queryByText("$958.38")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /Baseline control record/i }));
    expect(screen.getByText("$1,053.46 realized balance")).toBeInTheDocument();
    expect(screen.getByText(/curated, not deleted/i)).toBeInTheDocument();
  });

  it("preserves unknown daily fields instead of fabricating zero activity", () => {
    const s = {
      profiles: [
        {
          label: "Legacy live achieved performance",
          risk_profile: "live-legacy",
          profile_type: "primary",
          archived: true,
          paper_trading: { summary: { realized_pnl: 5 } },
          daily_summary: {
            window_start: "2026-08-02",
            window_end: "2026-08-02",
            days: [{ date: "2026-08-02", cumulative_realized: 5 }],
          },
        },
      ],
    } as unknown as StrategyLab;

    const { container } = render(<ArchivedPerformance s={s} />);
    const row = container.querySelector("tbody tr");

    expect(row).toBeInTheDocument();
    expect(row).toHaveTextContent("+$5.00");
    expect(row).not.toHaveTextContent("+$0.00");
    expect(row).not.toHaveTextContent("0–0");
    expect(row?.querySelectorAll("td")[0]).toHaveTextContent("—");
    expect(row?.querySelectorAll("td")[2]).toHaveTextContent("—");
    expect(row?.querySelectorAll("td")[3]).toHaveTextContent("—");
    expect(screen.getByText(/activity fields unavailable for this published run/i)).toBeInTheDocument();
  });
});
