import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PublicationProvider, type PublicationManifest } from "../../lib/publication";
import { PublicationLoaded } from "../../test/PublicationLoaded";
import type { ProfileEntry, StrategyLab } from "../../lib/strategy";
import { ProfileDashboard } from "./ProfileDashboard";

const profile = {
  label: "Candidate",
  risk_profile: "live",
  profile_type: "primary",
  paper_trading: {
    summary: {
      closed_positions: 4,
      win_count: 3,
      loss_count: 1,
      hit_rate: 0.75,
      realized_pnl: 5,
      roi: 0.1,
      open_positions: 1,
    },
  },
  status: { latest_signal_count: 2, paper_trading_status: "running" },
} as ProfileEntry;

const targetProfile = {
  ...profile,
  label: "Research target",
  risk_profile: "research-target",
  profile_type: "experimental",
  daily_target: {
    available: true,
    account_id: "paper-research-target-v1",
    target_pnl: 50,
    realized_pnl: 20,
    remaining_pnl: 30,
    achieved: false,
    locked: false,
    status: "miss",
    observed_days: 6,
    hit_count: 2,
    attainment_rate: 1 / 3,
    mean_daily_pnl: 8,
    median_daily_pnl: 5,
    independent_city_target_days: 11,
    resolution_days: 4,
    target_feasible: true,
    disclaimer: "Hard paper-research objective; not a guaranteed return.",
  },
} as ProfileEntry;

const strategy = {
  available: true,
  mode: "paper_research_only",
  paper_trading: {
    available: true,
    summary: { open_positions: 1 },
    open_positions: [
      {
        id: 1,
        label: "Alpha position",
        ticker: "KXHIGHTSFO-26JUL09-B68",
        risk_profile: "live",
      },
    ],
    closed_positions: [],
  },
} as unknown as StrategyLab;

const publication = (generatedAt: string): PublicationManifest => ({
  snapshot_id: "0123456789abcdef01234567",
  artifacts: {
    "strategy_research.json": { generated_at: generatedAt, sha256: "strategy", status: "ready" },
  },
});

const ok = (payload: unknown) =>
  ({ ok: true, status: 200, json: async () => payload }) as Response;

describe("ProfileDashboard publication truthfulness", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-09T12:00:00Z"));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    fetchMock.mockReset();
  });

  async function renderDashboard(generatedAt: string, value = profile, strategyValue = strategy) {
    fetchMock.mockResolvedValue(ok(publication(generatedAt)));
    render(
      <PublicationProvider>
        <PublicationLoaded artifacts={["strategy_research.json"]} />
        <ProfileDashboard s={strategyValue} p={value} />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));
  }

  it("withholds current profile status and candidate counts when stale", async () => {
    await renderDashboard("2026-07-07T12:00:00Z");

    expect(screen.queryByText("running")).not.toBeInTheDocument();
    expect(screen.getByText("Candidates now").parentElement).toHaveTextContent("Unavailable");
    expect(screen.queryByText("Alpha position")).not.toBeInTheDocument();
  });

  it("shows current profile state while Strategy Lab publication is fresh", async () => {
    await renderDashboard("2026-07-09T11:59:00Z");

    expect(screen.getByText("running")).toBeInTheDocument();
    expect(screen.getByText("Candidates now").parentElement).toHaveTextContent("2");
    expect(screen.getByText("Alpha position")).toBeInTheDocument();
  });

  it("orders profile history before the visible positions and execution log", async () => {
    const withHistory = {
      ...profile,
      daily_summary: {
        window_days: 2,
        days: [
          { date: "2026-07-08", cumulative_realized: 2, realized_pnl: 2 },
          { date: "2026-07-09", cumulative_realized: 5, realized_pnl: 3 },
        ],
      },
    } as ProfileEntry;

    await renderDashboard("2026-07-09T11:59:00Z", withHistory);

    const history = screen.getByText("Candidate — P&L contribution");
    const positions = screen.getByRole("heading", { name: "Positions & execution log" });
    expect(history.compareDocumentPosition(positions) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("keeps all published monthly closed positions together and initially shows five", async () => {
    const closedPositions = Array.from({ length: 22 }, (_, index) => ({
      id: index + 1,
      ticker: `TEST-${index + 1}`,
      label: `Closed position ${index + 1}`,
      side: "YES",
      contracts: 1,
      entry_price: 0.4,
      exit_price: 0.6,
      realized_pnl: 0.2,
      realized_roi: 0.5,
      quality_score: 80,
      risk_profile: "live",
      target_date: "2026-07-09",
      closed_at: new Date(Date.UTC(2026, 6, 22 - index)).toISOString(),
    }));
    const strategyWithLedger = {
      ...strategy,
      paper_trading: {
        ...strategy.paper_trading,
        closed_positions: closedPositions,
      },
    } as StrategyLab;
    const profileWithLedger = {
      ...profile,
      paper_trading: {
        summary: { ...profile.paper_trading?.summary, closed_positions: 22 },
      },
    } as ProfileEntry;

    await renderDashboard("2026-07-09T11:59:00Z", profileWithLedger, strategyWithLedger);

    expect(screen.getByRole("heading", { name: "Closed positions" })).toBeInTheDocument();
    expect(screen.getByText("Closed position 5")).toBeInTheDocument();
    expect(screen.queryByText("Closed position 6")).not.toBeInTheDocument();
    expect(screen.queryByText("Closed position 21")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Show 17 more closed positions" }));

    expect(screen.getByText("Closed position 22")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show fewer closed positions" })).toHaveAttribute("aria-expanded", "true");
  });

  it("shows the target account's fixed objective, evidence breadth, and non-guarantee", async () => {
    await renderDashboard("2026-07-09T11:59:00Z", targetProfile);

    expect(screen.getByText("Daily research objective")).toBeInTheDocument();
    expect(screen.getByText("$20.00 of $50.00")).toBeInTheDocument();
    expect(screen.getByText("Remaining").parentElement).toHaveTextContent("$30.00");
    expect(screen.getByText("Mean / median").parentElement).toHaveTextContent("+$8.00 / +$5.00");
    expect(screen.getByText("Observed / hit days").parentElement).toHaveTextContent("6 / 2");
    expect(screen.getByText("Independent city-target days").parentElement).toHaveTextContent("11");
    expect(screen.getByText("Feasible from current opportunities")).toBeInTheDocument();
    expect(screen.getByText("Target lock open")).toBeInTheDocument();
    expect(screen.getByText(/not a guaranteed return/i)).toBeInTheDocument();
  });

  it("distinguishes unknown feasibility from an infeasible target", async () => {
    const unknown = {
      ...targetProfile,
      daily_target: { ...targetProfile.daily_target, target_feasible: null },
    } as ProfileEntry;
    await renderDashboard("2026-07-09T11:59:00Z", unknown);

    expect(screen.getByText("Feasibility unknown")).toBeInTheDocument();
    expect(screen.queryByText("Not feasible from current opportunities")).not.toBeInTheDocument();
  });
});
