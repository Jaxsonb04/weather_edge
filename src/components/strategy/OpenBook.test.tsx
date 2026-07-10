import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PublicationProvider, type PublicationManifest } from "../../lib/publication";
import { PublicationLoaded } from "../../test/PublicationLoaded";
import type { StrategyLab } from "../../lib/strategy";
import { OpenBook } from "./OpenBook";

const strategy = {
  available: true,
  mode: "paper_research_only",
  paper_trading: {
    available: true,
    summary: {
      open_positions: 1,
      open_risk: 12,
      pending_limit_orders: 1,
      pending_limit_risk: 8,
      capital_at_risk: 20,
    },
    open_positions: [
      {
        id: 1,
        label: "Alpha position",
        ticker: "KXHIGHTSFO-26JUL09-B68",
        risk_profile: "live",
        risk: 12,
      },
    ],
    pending_limit_orders: [
      {
        id: 2,
        label: "Beta limit",
        ticker: "KXHIGHTSFO-26JUL10-B69",
        risk_profile: "live",
        risk: 8,
      },
    ],
  },
} as StrategyLab;

const publication = (generatedAt: string): PublicationManifest => ({
  snapshot_id: "0123456789abcdef01234567",
  artifacts: {
    "strategy_research.json": { generated_at: generatedAt, sha256: "strategy", status: "ready" },
  },
});

const ok = (payload: unknown) =>
  ({ ok: true, status: 200, json: async () => payload }) as Response;

describe("OpenBook publication truthfulness", () => {
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

  async function renderBook(generatedAt: string) {
    fetchMock.mockResolvedValue(ok(publication(generatedAt)));
    render(
      <PublicationProvider>
        <PublicationLoaded artifacts={["strategy_research.json"]} />
        <OpenBook s={strategy} />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));
  }

  it("suppresses stale open and pending counts and lists", async () => {
    await renderBook("2026-07-07T12:00:00Z");

    expect(screen.queryByText("Alpha position")).not.toBeInTheDocument();
    expect(screen.queryByText("Beta limit")).not.toBeInTheDocument();
    expect(screen.getAllByText("Unavailable").length).toBeGreaterThanOrEqual(4);
    expect(screen.getByText(/current open and pending book state is unavailable/i)).toBeInTheDocument();
  });

  it("shows open and pending state while Strategy Lab publication is fresh", async () => {
    await renderBook("2026-07-09T11:59:00Z");

    expect(screen.getByText("Alpha position")).toBeInTheDocument();
    expect(screen.getByText("Beta limit")).toBeInTheDocument();
  });
});
