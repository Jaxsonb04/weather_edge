import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PublicationProvider, type PublicationManifest } from "../../lib/publication";
import { PublicationLoaded } from "../../test/PublicationLoaded";
import type { StrategyLab } from "../../lib/strategy";
import { OpenBook } from "./OpenBook";

/** Regression: a book with more than five open positions folded the overflow
    into its own bordered surface nested inside the "Open positions" card, so
    expanding it appeared to spawn a second block instead of continuing the
    list. The overflow rows must extend the existing list, not open a new
    surface. */

const longBook = (count: number) =>
  ({
    available: true,
    mode: "paper_research_only",
    paper_trading: {
      available: true,
      summary: {
        open_positions: count,
        open_risk: count,
        pending_limit_orders: 0,
        pending_limit_risk: 0,
        capital_at_risk: count,
      },
      open_positions: Array.from({ length: count }, (_, index) => ({
        id: index + 1,
        label: `Position ${index + 1}`,
        ticker: "KXHIGHTSFO-26JUL09-B68",
        risk_profile: "live",
        risk: 1,
      })),
      pending_limit_orders: [],
    },
  }) as unknown as StrategyLab;

const publication = (generatedAt: string): PublicationManifest => ({
  snapshot_id: "0123456789abcdef01234567",
  artifacts: {
    "strategy_research.json": { generated_at: generatedAt, sha256: "strategy", status: "ready" },
  },
});

const ok = (payload: unknown) => ({ ok: true, status: 200, json: async () => payload }) as Response;

describe("OpenBook overflow disclosure", () => {
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

  async function renderBook(count: number) {
    fetchMock.mockResolvedValue(ok(publication("2026-07-09T11:59:00Z")));
    render(
      <PublicationProvider>
        <PublicationLoaded artifacts={["strategy_research.json"]} />
        <OpenBook s={longBook(count)} profile="live" />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));
  }

  it("keeps every open position in a single list once expanded", async () => {
    await renderBook(12);

    const trigger = screen.getByRole("button", { name: /show 7 more open positions/i });
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    // Collapsed: only the first five rows are rendered.
    expect(screen.getByText("Position 5")).toBeInTheDocument();
    expect(screen.queryByText("Position 6")).not.toBeInTheDocument();

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");

    // Expanded: all twelve rows share ONE list, so the overflow reads as a
    // continuation rather than a separate block.
    const first = screen.getByText("Position 1").closest("li");
    const last = screen.getByText("Position 12").closest("li");
    expect(first).not.toBeNull();
    expect(last).not.toBeNull();
    expect(last?.closest("ul")).toBe(first?.closest("ul"));
  });

  it("does not nest a bordered surface inside the open positions card", async () => {
    await renderBook(12);

    fireEvent.click(screen.getByRole("button", { name: /show 7 more open positions/i }));

    const row = screen.getByText("Position 12").closest("li");
    const list = row?.closest("ul");
    expect(list).not.toBeNull();

    // Walk from the overflow row up to the card. Nothing along the way may
    // introduce its own ring/border/background surface.
    const surfaces: string[] = [];
    for (let node = list?.parentElement ?? null; node; node = node.parentElement) {
      if (node.classList.contains("card")) break;
      const cls = node.className;
      if (typeof cls === "string" && /(^|\s)(ring-1|border(\s|$)|bg-surface)/.test(cls)) {
        surfaces.push(cls);
      }
    }
    expect(surfaces).toEqual([]);
  });

  it("leaves short books unfolded", async () => {
    await renderBook(4);

    expect(screen.queryByRole("button", { name: /more open positions/i })).not.toBeInTheDocument();
    expect(screen.getByText("Position 4")).toBeInTheDocument();
  });
});
