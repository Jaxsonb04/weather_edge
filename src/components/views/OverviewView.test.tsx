import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { DashboardData } from "../../lib/data";

const belowFoldModule = vi.hoisted(() => ({ loads: 0 }));

vi.mock("./OverviewBelowFold", () => {
  belowFoldModule.loads += 1;
  return { OverviewBelowFold: () => <div>Deferred overview content</div> };
});

import { BelowFoldBoundary } from "./OverviewView";

type ObserverCallback = IntersectionObserverCallback;

let callback: ObserverCallback;
let observed: Element | null;
let options: IntersectionObserverInit | undefined;

beforeEach(() => {
  belowFoldModule.loads = 0;
  observed = null;
  options = undefined;

  class MockIntersectionObserver {
    constructor(nextCallback: ObserverCallback, nextOptions?: IntersectionObserverInit) {
      callback = nextCallback;
      options = nextOptions;
    }

    observe(element: Element) {
      observed = element;
    }

    disconnect() {}
    unobserve() {}
    takeRecords(): IntersectionObserverEntry[] { return []; }
    readonly root = null;
    readonly rootMargin = "0px";
    readonly thresholds = [0];
  }

  vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
});

describe("overview below-fold loading boundary", () => {
  it("reserves layout without importing the below-fold module on initial render", () => {
    render(<BelowFoldBoundary data={{} as DashboardData} />);

    expect(screen.getByRole("status", { name: "Loading overview instruments" })).toBeInTheDocument();
    expect(screen.queryByText("Deferred overview content")).not.toBeInTheDocument();
    expect(belowFoldModule.loads).toBe(0);
    expect(observed).toBe(screen.getByTestId("overview-below-fold-sentinel"));
    expect(options).toMatchObject({ rootMargin: "0px", threshold: 0 });
  });

  it("imports and renders the below-fold module after its sentinel intersects", async () => {
    render(<BelowFoldBoundary data={{} as DashboardData} />);

    await act(async () => {
      callback([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver);
    });

    expect(await screen.findByText("Deferred overview content")).toBeInTheDocument();
    expect(belowFoldModule.loads).toBe(1);
  });
});
