import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DashboardData } from "../../lib/data";

vi.mock("./OverviewBelowFold", () => ({
  OverviewBelowFold: () => <div>Guaranteed overview content</div>,
}));

import { BelowFoldBoundary } from "./OverviewView";

// Regression: ISSUE-002 — jumping past the sentinel left the Overview blank.
// Report: .gstack/qa-reports/qa-report-jaxsonb04-github-io-2026-07-20.md
describe("overview below-fold fallback", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "IntersectionObserver",
      class {
        observe() {}
        disconnect() {}
        unobserve() {}
        takeRecords(): IntersectionObserverEntry[] { return []; }
        readonly root = null;
        readonly rootMargin = "0px";
        readonly thresholds = [0];
      },
    );
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("mounts the content even when the sentinel never intersects", async () => {
    render(<BelowFoldBoundary data={{} as DashboardData} />);

    expect(screen.getByRole("status", { name: "Loading overview instruments" })).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500);
      await Promise.resolve();
    });

    expect(screen.getByText("Guaranteed overview content")).toBeInTheDocument();
  });
});
