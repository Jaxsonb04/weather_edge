import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { M1Book, StrategyLab } from "../../lib/strategy";
import { M1Trial } from "./M1Trial";

vi.mock("../../lib/publication", () => ({ usePublication: () => ({ strategy: { state: "fresh" } }) }));

const lab = (research_m1?: unknown) =>
  ({ available: true, accounting: { accounts: { live: { realized_equity: 1031.4 }, research_m1 } } }) as unknown as StrategyLab;

const book: M1Book = {
  available: true,
  initial_equity: 2_000,
  realized_equity: 2_010.49,
  realized_pnl: 10.49,
  open_positions: 2,
  trial: {
    entries_enabled_by_stop_rules: true,
    promotion_note: "Nothing is promoted to real money from this run alone.",
    book: { entries: 2, settled_entries: 2, settled_stake: 20 },
    stop_rules: [
      { rule: "loss_stop", label: "Cumulative loss stop", status: "ok", threshold_dollars: -300, realized_pnl: 10.49 },
      { rule: "final_read", label: "Final read", status: "running", entries: 2, entries_limit: 1500, trading_days: 1, trading_days_limit: 60 },
    ],
    hypotheses: [{ id: "H1", statement: "M1 ROI > 0", alpha: 0.025, roi: 0.5245, lower: null, upper: null, settled_entries: 2 }],
    caps: { target_date_cap_dollars: 100 },
    rule: { edge_threshold: 0.02, lead_days: 1, exits: "hold to settlement" },
    shadow_arms: { m1_5min: { arm: "m1_5min", entries: 3, settled_entries: 1, roi: null } },
  },
};

describe("M1Trial", () => {
  it("shows an empty state, not zeros, before the runtime publishes the book", () => {
    render(<M1Trial s={lab()} />);
    expect(screen.getByText("No M1 data published yet")).toBeInTheDocument();
    expect(screen.getByText(/\$2,000 paper book/)).toBeInTheDocument();
    expect(screen.queryByText("Realized P&L")).not.toBeInTheDocument();
  });

  it("shows the publisher's reason when the ledger is unreadable", () => {
    render(<M1Trial s={lab({ available: false, reason: "M1 trial ledger unavailable (ZeroDivisionError)" })} />);
    expect(screen.getByText(/ZeroDivisionError/)).toBeInTheDocument();
  });

  it("reads the book on its own, never with another account's balance", () => {
    render(<M1Trial s={lab(book)} />);
    expect(screen.getAllByText("$2,010.49").length).toBeGreaterThan(0);
    expect(screen.getByText("+$10.49")).toBeInTheDocument();
    expect(screen.getByText(/never added to any other account/)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("3,041.89");
  });

  it("names the stop rules and progress in plain words", () => {
    render(<M1Trial s={lab(book)} />);
    expect(screen.getByText("Stops at −$300.00; realized +$10.49")).toBeInTheDocument();
    expect(screen.getByText("Clear")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Entries" })).toHaveAttribute("aria-valuemax", "1500");
  });

  it("charts only days with a published closing balance", () => {
    render(<M1Trial s={lab({
      ...book,
      days: [
        { date: "2026-11-04", cumulative_realized: 0, closing_equity: null },
        { date: "2026-11-05", cumulative_realized: 0 },
        { date: "2026-11-06", cumulative_realized: 0, closing_equity: 2000 },
        { date: "2026-11-07", cumulative_realized: 10.49, closing_equity: 2010.49 },
      ],
    })} />);

    expect(screen.getByRole("img", { name: /Balance over 2 days, from \$2,000 to \$2,010/ })).toBeInTheDocument();
  });

  it("shows a one-line empty state instead of a flat balance line before the first entry", () => {
    render(<M1Trial s={lab({
      ...book,
      realized_equity: 2000,
      trial: { ...book.trial, book: { entries: 0, settled_entries: 0 } },
      days: [
        { date: "2026-11-06", cumulative_realized: 0, closing_equity: 2000 },
        { date: "2026-11-07", cumulative_realized: 0, closing_equity: 2000 },
      ],
    })} />);

    expect(screen.getByText(/No entries yet/)).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: /Balance over/ })).not.toBeInTheDocument();
  });

  it("withholds every figure from a populated book the publisher could not verify", () => {
    render(<M1Trial s={lab({ ...book, available: false, reason: "M1 trial ledger is not active or not reconciled" })} />);
    expect(screen.getByText(/not active or not reconciled/)).toBeInTheDocument();
    expect(screen.queryByText(/2,010/)).not.toBeInTheDocument();
    expect(screen.queryByText("Paper book")).not.toBeInTheDocument();
  });

  it("never writes the venue's name, even though the payload does", () => {
    const named = { ...book, trial: { ...book.trial, rule: { ...book.trial?.rule, execution: "per-order Kalshi fee" } } };
    render(<M1Trial s={lab(named)} />);
    expect(document.body.textContent).not.toMatch(/kalshi/i);
  });
});
