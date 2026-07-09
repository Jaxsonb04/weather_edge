import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PublicationProvider, type PublicationManifest } from "../../lib/publication";
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

  async function renderDashboard(generatedAt: string) {
    fetchMock.mockResolvedValue(ok(publication(generatedAt)));
    render(
      <PublicationProvider>
        <ProfileDashboard s={strategy} p={profile} />
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
});
