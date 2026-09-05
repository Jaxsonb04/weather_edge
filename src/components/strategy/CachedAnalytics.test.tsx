import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { StrategyLab } from "../../lib/strategy";
import { GateFunnel } from "./GateFunnel";
import { OpsHealth } from "./OpsHealth";

const strategy = {
  daily_summary: {
    decision_analytics: {
      status: "cached",
      counts_stale_from: "2026-09-01",
    },
    data_collected: {
      decision_snapshots: 848868,
      paper_orders: 29,
    },
    gate_behavior: {
      approved: 20,
      rejected: 80,
      top_rejections: [],
      by_profile: [],
    },
  },
} as unknown as StrategyLab;

describe("cached decision analytics labels", () => {
  it("dates runtime collection counters", () => {
    render(<OpsHealth s={strategy} />);
    expect(screen.getByRole("status")).toHaveTextContent(
      /historical counts as of 2026-09-01/i,
    );
  });

  it("dates gate-funnel counts", () => {
    render(<GateFunnel s={strategy} />);
    expect(screen.getByText(/cached gate counts as of 2026-09-01/i)).toBeInTheDocument();
  });
});
