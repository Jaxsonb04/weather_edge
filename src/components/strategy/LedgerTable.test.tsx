import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LedgerTable } from "./LedgerTable";
import type { ClosedPosition, StrategyLab } from "../../lib/strategy";

const trade: ClosedPosition = {
  id: 1, ticker: "KXHIGHTSFO-26SEP07-B68", label: "68° to 69°", side: "YES",
  contracts: 5, entry_price: 0.4, exit_price: 0.6, realized_pnl: 1, realized_roi: 0.5,
  risk_profile: "live", target_date: "2026-09-07", closed_at: "2026-09-08T01:30:00Z",
  quality_score: 82, edge: 0.07, settlement_high_f: 69,
  outcome_reason: "Closed at the published exit quote.",
};
const strategy = {} as StrategyLab;

function mobile() {
  const original = window.matchMedia;
  vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
    ...original(query), matches: query === "(max-width: 767px)",
  }));
}
afterEach(() => vi.restoreAllMocks());

describe("closed-position browsing", () => {
  it("shows one vertical mobile record with P&L and a tap-accessible trade detail panel", () => {
    mobile();
    render(<LedgerTable s={strategy} rows={[trade]} detailed hideProfile />);
    expect(screen.queryByRole("grid")).not.toBeInTheDocument();
    const trigger = screen.getByRole("button", { name: /68° to 69°/ });
    expect(trigger).toHaveTextContent("San Francisco");
    expect(trigger).toHaveTextContent("+$1.00");
    expect(trigger).toHaveTextContent("Win");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    const panel = document.getElementById(trigger.getAttribute("aria-controls")!)!;
    expect(within(panel).getByText("40¢ → 60¢")).toBeInTheDocument();
    expect(within(panel).getByText("Sep 8, 01:30")).toBeInTheDocument();
    expect(within(panel).getByText("+7.0%")).toBeInTheDocument();
    expect(within(panel).getByText("69°F")).toBeInTheDocument();
    expect(within(panel).getByText(trade.outcome_reason!)).toBeInTheDocument();
  });

  it("keeps absent detail values unavailable and labels zero P&L flat", () => {
    mobile();
    render(<LedgerTable s={strategy} rows={[{ ...trade, realized_pnl: 0, edge: undefined, exit_price: null, closed_at: null }]} detailed />);
    const trigger = screen.getByRole("button", { name: /68° to 69°/ });
    expect(trigger).toHaveTextContent("Flat");
    fireEvent.click(trigger);
    expect(screen.getByText("40¢ → —")).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2);
  });

  it("keeps the publication's explicit outcome tone authoritative", () => {
    mobile();
    render(<LedgerTable s={strategy} rows={[{ ...trade, realized_pnl: 0.01, position_status_tone: "warn" }]} />);
    expect(screen.getByRole("button", { name: /68° to 69°/ })).toHaveTextContent("Flat");
  });

  it("uses a Pro desktop grid with pinned context and progressively disclosed evidence columns", () => {
    render(<LedgerTable s={strategy} rows={[trade]} detailed hideProfile />);
    const grid = screen.getByRole("grid");
    expect(screen.queryByRole("columnheader", { name: "Quality" })).not.toBeInTheDocument();
    expect(within(grid).getByRole("columnheader", { name: "Bracket" })).toHaveAttribute("data-pinned", "start");
    expect(within(grid).getByRole("columnheader", { name: "P&L" })).toHaveAttribute("data-pinned", "end");
    fireEvent.click(screen.getByRole("button", { name: "All trade details" }));
    expect(screen.getByRole("columnheader", { name: "Quality" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Essential columns" })).toHaveAttribute("aria-pressed", "true");
  });
});
