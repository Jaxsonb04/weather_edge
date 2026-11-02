import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { StrategyLab } from "../../lib/strategy";
import { percent } from "../../lib/lab-format";
import { ExitStudy } from "./ExitStudy";

// The all-time diagnostics key exit routes without the `closed_` prefix the
// weekly counts use, and they pool separate accounts, so only counts and
// ratios may be rendered from them.
const lab = {
  paper_trading: {
    diagnostics: {
      by_exit_reason: {
        held_to_settlement: { positions: 612, resolved: 602, wins: 593, losses: 9, hit_rate: 0.985, roi: 0.1322, realized_pnl: 538.25 },
        take_profit: { positions: 269, resolved: 269, wins: 267, losses: 1, hit_rate: 0.9963, roi: 0.1732, realized_pnl: 338.82 },
        stop_loss: { positions: 402, resolved: 402, wins: 0, losses: 402, hit_rate: 0, roi: -0.4015, realized_pnl: -698.62 },
        expired_unfilled: { positions: 2053, resolved: 0, wins: 0, losses: 0, hit_rate: null, roi: null, realized_pnl: 0 },
      },
    },
  },
} as unknown as StrategyLab;

describe("ExitStudy", () => {
  it("reads every all-time exit route under its bare key", () => {
    render(<ExitStudy s={lab} />);
    expect(screen.getByText("Held to settlement")).toBeInTheDocument();
    expect(screen.getByText("Take-profit")).toBeInTheDocument();
    expect(screen.getByText("Stop-loss")).toBeInTheDocument();
    expect(screen.getByText(/2,053 limit orders expired unfilled/)).toBeInTheDocument();
    expect(screen.getByText("98.5%", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByText("0%", { selector: "strong" })).toBeInTheDocument();
  });

  it("never renders a dollar sum pooled across separate accounts", () => {
    const { container } = render(<ExitStudy s={lab} />);
    expect(container.textContent).not.toMatch(/\$/);
    expect(container.textContent).toContain("ROI −40.2%");
  });
});

describe("percent", () => {
  it("uses a true minus sign and an optional plus, like money()", () => {
    expect(percent(-0.0303, 1)).toBe("−3.0%");
    expect(percent(0.0374, 1, "always")).toBe("+3.7%");
    expect(percent(0.0264, 1)).toBe("2.6%");
    expect(percent(-0.00001, 1)).toBe("0.0%");
    expect(percent(null)).toBe("—");
  });
});
