import { act, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PublicationProvider } from "../../lib/publication";
import type { StrategyLab } from "../../lib/strategy";
import { PublicationLoaded } from "../../test/PublicationLoaded";
import { AccountCards } from "./AccountCards";
import { M1Trial } from "./M1Trial";

const lab = {
  available: true,
  accounting: { available: true, accounts: { research_m1: {
    available: true, realized_equity: 2010, open_positions: 3, open_cost_basis: 12,
    trial: {
      entries_enabled_by_stop_rules: true,
      stop_rules: [{ rule: "loss_stop", label: "Cumulative loss stop", status: "ok" }],
    },
  } } },
  profiles: [{
    risk_profile: "live", label: "Live Stability", profile_type: "primary",
    daily_summary: { current_equity: 2020 },
    paper_trading: { summary: { open_positions: 2, pending_limit_orders: 4 } },
  }],
} as unknown as StrategyLab;

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("current paper book freshness", () => {
  it.each(["fresh", "stale", "unknown"])("gates AccountCards and M1 current state for a %s publication", async (state) => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-11-07T12:00:00Z"));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({
      snapshot_id: "0123456789abcdef01234567",
      artifacts: state === "unknown" ? {} : { "strategy_research.json": {
        generated_at: state === "fresh" ? "2026-11-07T11:59:00Z" : "2026-11-06T12:00:00Z",
        sha256: "strategy", status: "ready",
      } },
    }) }));
    render(
      <PublicationProvider>
        <PublicationLoaded artifacts={["strategy_research.json"]} />
        <AccountCards s={lab} selected="live" onSelect={() => {}} />
        <M1Trial s={lab} />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));

    const card = screen.getByRole("button", { name: /Live Stability/ });
    const m1Open = screen.getAllByText("Open").find((label) => label.tagName === "P")!;
    if (state === "fresh") {
      expect(card).toHaveTextContent("Scanning");
      expect(card).toHaveTextContent("4 resting");
      expect(within(card).getByText("Open").nextElementSibling).toHaveTextContent(/^2$/);
      expect(m1Open.nextElementSibling).toHaveTextContent(/^3$/);
      expect(screen.getByText("$12.00 at risk")).toBeInTheDocument();
      expect(screen.getByText("Stop rules clear")).toBeInTheDocument();
      expect(screen.getByText("Clear")).toBeInTheDocument();
    } else {
      expect(card).toHaveTextContent("Current status unavailable");
      expect(card).toHaveTextContent("— resting");
      expect(card).not.toHaveTextContent("Scanning");
      expect(card).not.toHaveTextContent("4 resting");
      expect(within(card).getByText("Open").nextElementSibling).toHaveTextContent(/^—$/);
      expect(m1Open.nextElementSibling).toHaveTextContent("Unavailable");
      expect(screen.queryByText("$12.00 at risk")).not.toBeInTheDocument();
      expect(screen.queryByText("Stop rules clear")).not.toBeInTheDocument();
      expect(screen.queryByText("Clear")).not.toBeInTheDocument();
    }
    expect(card).toHaveTextContent("$2,020.00");
    expect(screen.getAllByText("$2,010.00").length).toBeGreaterThan(0);
  });
});
