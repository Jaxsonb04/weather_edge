import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PublicationProvider, type PublicationManifest } from "../../lib/publication";
import { PublicationLoaded } from "../../test/PublicationLoaded";
import type { StrategyLab } from "../../lib/strategy";
import { ProfileComparison } from "./ProfileComparison";

const strategy = {
  available: true,
  mode: "paper_research_only",
  profiles: [
    {
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
          open_positions: 3,
        },
      },
      status: { latest_signal_count: 2, paper_trading_status: "running" },
    },
    {
      label: "Research",
      risk_profile: "research",
      profile_type: "experimental",
      paper_trading: {
        summary: {
          closed_positions: 5,
          win_count: 3,
          loss_count: 2,
          hit_rate: 0.6,
          realized_pnl: 4,
          roi: 0.08,
          open_positions: 4,
        },
      },
      status: { latest_signal_count: 1, paper_trading_status: "running" },
    },
  ],
} as StrategyLab;

const canonicalStrategy = {
  ...strategy,
  profiles: [
    {
      label: "Research motion",
      risk_profile: "research-motion",
      profile_type: "experimental",
      paper_trading: { summary: { closed_positions: 1, win_count: 0, loss_count: 1, hit_rate: 0, realized_pnl: -2, roi: -0.02, open_positions: 0 } },
      status: { latest_signal_count: 5, paper_trading_status: "high activity" },
      excluded_from: ["daily_target", "live_readiness"],
    },
    strategy.profiles![0],
    {
      label: "Legacy research should not render",
      risk_profile: "research",
      profile_type: "experimental",
    },
    {
      label: "Research target",
      risk_profile: "research-target",
      profile_type: "experimental",
      paper_trading: { summary: { closed_positions: 2, win_count: 1, loss_count: 1, hit_rate: 0.5, realized_pnl: 18, roi: 0.03, open_positions: 1 } },
      status: { latest_signal_count: 3, paper_trading_status: "tracking objective" },
      daily_target: {
        available: true,
        target_pnl: 50,
        realized_pnl: 18,
        remaining_pnl: 32,
        achieved: false,
        locked: false,
        target_feasible: false,
      },
    },
  ],
} as StrategyLab;

const publication = (generatedAt: string): PublicationManifest => ({
  snapshot_id: "0123456789abcdef01234567",
  artifacts: {
    "strategy_research.json": { generated_at: generatedAt, sha256: "strategy", status: "ready" },
  },
});

const ok = (payload: unknown) =>
  ({ ok: true, status: 200, json: async () => payload }) as Response;

describe("ProfileComparison publication truthfulness", () => {
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

  async function renderComparison(generatedAt: string, value = strategy) {
    fetchMock.mockResolvedValue(ok(publication(generatedAt)));
    render(
      <PublicationProvider>
        <PublicationLoaded artifacts={["strategy_research.json"]} />
        <ProfileComparison s={value} />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));
  }

  it("marks current profile counts unavailable when Strategy Lab publication is stale", async () => {
    await renderComparison("2026-07-07T12:00:00Z");

    expect(screen.getAllByText("Unavailable")).toHaveLength(4);
    expect(screen.queryByText("running")).not.toBeInTheDocument();
  });

  it("keeps current profile counts visible while Strategy Lab publication is fresh", async () => {
    await renderComparison("2026-07-09T11:59:00Z");

    expect(screen.getAllByText("Open now")[0].parentElement).toHaveTextContent("3");
    expect(screen.getAllByText("Candidates this scan")[0].parentElement).toHaveTextContent("2");
  });

  it("renders only canonical books in live-target-motion order with target and exclusion evidence", async () => {
    await renderComparison("2026-07-09T11:59:00Z", canonicalStrategy);

    const page = document.body.textContent ?? "";
    expect(page.indexOf("Candidate")).toBeLessThan(page.indexOf("Research target"));
    expect(page.indexOf("Research target")).toBeLessThan(page.indexOf("Research motion"));
    expect(screen.queryByText("Legacy research should not render")).not.toBeInTheDocument();
    expect(screen.getByText("$50.00 daily target")).toBeInTheDocument();
    expect(screen.getByText("Not feasible from current opportunities")).toBeInTheDocument();
    expect(screen.getByText("Excluded from daily target and live readiness")).toBeInTheDocument();
  });
});
