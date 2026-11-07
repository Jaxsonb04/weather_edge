import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { StrategyLab } from "../../lib/strategy";
import { OpsHealth } from "./OpsHealth";

const lab = (extra: Record<string, unknown> = {}) =>
  ({
    daily_summary: {
      window_start: "2026-11-01",
      window_end: "2026-11-07",
      data_collected: { paper_orders: 92 },
      decision_analytics: { status: "cached", counts_stale_from: "2026-11-06" },
    },
    ...extra,
  }) as unknown as StrategyLab;

describe("OpsHealth", () => {
  it("labels the collected counts as the published window, not a total", () => {
    render(<OpsHealth s={lab()} />);
    expect(screen.getByText(/in the window Nov 1 – Nov 7, not all-time totals/)).toBeInTheDocument();
    expect(screen.getByText(/Window counts as of 2026-11-06/)).toBeInTheDocument();
    expect(screen.queryByText(/Historical counts/)).not.toBeInTheDocument();
  });

  it("stays true when no window is published", () => {
    render(<OpsHealth s={{ daily_summary: { data_collected: { paper_orders: 92 } } } as unknown as StrategyLab} />);
    expect(screen.getByText(/in the recent reporting window, not all-time totals/)).toBeInTheDocument();
  });

  it("never shows a negative observation lag", () => {
    const feed = (lag_days: number) =>
      lab({ forecast_health: { available: true, nws_ground_truth: { available: true, latest_date: "2026-11-07", lag_days } } });
    const { unmount } = render(<OpsHealth s={feed(-1)} />);
    expect(screen.getByText("latest 2026-11-07 · current")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/-1d/);
    unmount();
    render(<OpsHealth s={feed(1)} />);
    expect(screen.getByText("latest 2026-11-07 · 1d lag")).toBeInTheDocument();
  });
});
