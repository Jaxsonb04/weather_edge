import { act, fireEvent, render, screen } from "@testing-library/react";
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

  async function renderBook(generatedAt: string, value = strategy, profile?: string) {
    fetchMock.mockResolvedValue(ok(publication(generatedAt)));
    render(
      <PublicationProvider>
        <PublicationLoaded artifacts={["strategy_research.json"]} />
        <OpenBook s={value} profile={profile} />
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

  it("uses profile totals when the global row cap omits some or all of its rows", async () => {
    const capped = {
      ...strategy,
      profiles: [{
        risk_profile: "live",
        paper_trading: { summary: { open_positions: 2, open_risk: 24, pending_limit_orders: 1, pending_limit_risk: 8 } },
      }],
      paper_trading: {
        ...strategy.paper_trading,
        open_positions: [
          ...strategy.paper_trading.open_positions!,
          ...Array.from({ length: 29 }, (_, i) => ({ id: i + 10, risk_profile: "research-target", risk: 30 })),
        ],
        pending_limit_orders: [{ id: 50, risk_profile: "research-target", risk: 30 }],
      },
    } as unknown as StrategyLab;

    await renderBook("2026-07-09T11:59:00Z", capped, "live");

    expect(screen.getByText("Open positions", { selector: "p" }).nextElementSibling).toHaveTextContent(/^2$/);
    expect(screen.getByText("Open risk").nextElementSibling).toHaveTextContent("$24.00");
    expect(screen.getByText("Pending limits").nextElementSibling).toHaveTextContent(/^1$/);
    expect(screen.getByText("Pending risk").nextElementSibling).toHaveTextContent("$8.00");
    expect(screen.getByText(/showing 1 of 2 open positions/i)).toBeInTheDocument();
    expect(screen.getByText(/showing 0 of 1 pending limits/i)).toBeInTheDocument();
    expect(screen.queryByText("No pending limit orders.")).not.toBeInTheDocument();
    expect(screen.queryByText(/everything has closed or settled/i)).not.toBeInTheDocument();
  });

  it("keeps equal desktop gutters around the positions divider", async () => {
    await renderBook("2026-07-09T11:59:00Z");

    const openSection = screen.getByRole("heading", { name: "Open positions" }).closest("section");
    const pendingSection = screen.getByRole("heading", { name: "Pending limit orders" }).closest("section");
    const columns = openSection?.parentElement;

    expect(openSection).toHaveClass("lg:pr-8");
    expect(pendingSection).toHaveClass("lg:pl-8");
    expect(columns).not.toHaveClass("gap-x-8");
  });

  it("keeps an empty pending-order column compact", async () => {
    const emptyPending = {
      ...strategy,
      paper_trading: {
        ...strategy.paper_trading,
        summary: {
          ...strategy.paper_trading?.summary,
          pending_limit_orders: 0,
          pending_limit_risk: 0,
        },
        pending_limit_orders: [],
      },
    } as StrategyLab;

    await renderBook("2026-07-09T11:59:00Z", emptyPending);

    const message = screen.getByText("No pending limit orders.");
    expect(message.parentElement).toHaveClass("py-3");
    expect(message.parentElement).not.toHaveClass("py-6");
    expect(screen.queryByText(/engine posts limits/i)).not.toBeInTheDocument();
  });

  it("keeps five position rows visible and folds longer books", async () => {
    const longBook = {
      ...strategy,
      paper_trading: {
        ...strategy.paper_trading,
        open_positions: Array.from({ length: 7 }, (_, index) => ({
          id: index + 10,
          label: `Position ${index + 1}`,
          ticker: "KXHIGHTSFO-26JUL09-B68",
          risk_profile: "live",
          risk: 1,
        })),
      },
    } as StrategyLab;

    await renderBook("2026-07-09T11:59:00Z", longBook);

    const trigger = screen.getByRole("button", { name: "Show 2 more open positions" });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
  });
});
