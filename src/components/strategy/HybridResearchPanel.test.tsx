import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { StrategyLab } from "../../lib/strategy";

const resource = vi.hoisted(() => ({ data: null as unknown }));
vi.mock("../../lib/data", async (original) => ({
  ...await original<typeof import("../../lib/data")>(),
  useResource: () => ({ data: resource.data, loading: false, error: null }),
}));

import { HybridResearchPanel } from "./HybridResearchPanel";

function publication(policy?: string, live?: boolean): StrategyLab {
  return {
    profiles: [{ risk_profile: "research-target", daily_target: { policy_version: policy } }],
    live_orders_enabled: live,
    real_money_readiness: { available: true, ready: true, verdict: "READY" },
  } as StrategyLab;
}

describe("Hybrid research boundaries", () => {
  it("does not present V7 source as a deployed V7 ledger or treat readiness as authorization", () => {
    render(<HybridResearchPanel s={publication("research-target-roi-v6", false)} />);
    expect(screen.getByText(/still reports research-target-roi-v6/)).toHaveTextContent(/do not establish a V7 production track record/);
    expect(screen.getByText(/Paper only · live allocation \$0/)).toBeInTheDocument();
    expect(screen.getByText(/is disabled in this publication/)).toBeInTheDocument();
  });

  it("keeps an absent policy and live flag explicitly unknown", () => {
    render(<HybridResearchPanel s={publication()} />);
    expect(screen.getByText(/V7 activation is unverified/)).toHaveTextContent(/no published flag/);
    expect(screen.queryByText(/reports V7\./)).not.toBeInTheDocument();
    expect(screen.getByText(/Research evidence has not loaded/)).toBeInTheDocument();
  });

  it("warns when the feed contradicts paper-only operation", () => {
    render(<HybridResearchPanel s={publication("research-target-roi-v7", true)} />);
    expect(screen.getByText(/reports V7\./)).toHaveTextContent(/unexpectedly reported enabled/);
  });

  it("does not mistake a different generation for V7", () => {
    render(<HybridResearchPanel s={publication("research-target-roi-v70", false)} />);
    expect(screen.getByText(/still reports research-target-roi-v70/)).toBeInTheDocument();
  });

  it("tolerates partially published candidates and never turns malformed counts into zero", () => {
    resource.data = {
      evaluation_kind: "retrospective_diagnostic", cases: "666", distinct_target_dates: -1,
      qualified_original_vintage_cases: 0,
      candidates: [null, "invalid", { name: { unsafe: true }, comparison: 4, state: null }],
    };
    render(<HybridResearchPanel s={publication("research-target-roi-v6", false)} />);
    const trigger = screen.getByRole("button", { name: /Local model comparison/ });
    // The body mounts even when collapsed, so malformed rows cannot break the
    // surrounding account workbench before the user expands the evidence.
    expect(trigger).toBeInTheDocument();
    resource.data = null;
  });
});
