import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { gateDeferred, type StrategyLab } from "../../lib/strategy";
import { GateFunnel } from "./GateFunnel";
import { OpsHealth } from "./OpsHealth";

vi.mock("../ui/Reveal", () => ({
  Reveal: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));


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
    model_vs_market: { samples: 211221, mean_abs_gap: 0.0665 },
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
      /window counts as of 2026-09-01/i,
    );
  });

  it("dates gate-funnel counts", () => {
    render(<GateFunnel s={strategy} />);
    expect(screen.getByText(/cached gate counts as of 2026-09-01/i)).toBeInTheDocument();
    expect(screen.getByText(/gate evaluations in the window ending 2026-09-01/i)).toBeInTheDocument();
  });

  it("keeps the model-vs-market sentence grammatical under a cached cutoff", () => {
    render(<OpsHealth s={strategy} />);
    expect(
      screen.getByText(/as of that cutoff, the model-vs-market gap had been tracked/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/gap was across/i)).not.toBeInTheDocument();
  });

  // The selectivity claim now lives only in the gate filter, which must carry
  // the cached marker and never fall back to "this window" wording.
  it("never words cached gate counts as a live window", () => {
    render(<GateFunnel s={strategy} />);
    expect(screen.getByText(/gate evaluations in the window ending 2026-09-01/i)).toBeInTheDocument();
    expect(screen.queryByText(/gate evaluations this window/i)).not.toBeInTheDocument();
  });

  it("treats a published deferred status as authoritative over the gate stub", () => {
    const gate = { approved: 20, rejected: 80, by_profile: [] };
    expect(gateDeferred(gate)).toBe(false);
    expect(gateDeferred(gate, { status: "deferred" })).toBe(true);
    expect(gateDeferred(gate, { status: "cached" })).toBe(false);
  });
});
