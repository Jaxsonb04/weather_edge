import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const routeState = vi.hoisted(() => ({ route: "overview" as "overview" | "methodology" | "lab" }));
const dashboardState = vi.hoisted(() => ({
  data: { signal: { disclaimer: "Paper only" } } as unknown,
  error: null as string | null,
}));

vi.mock("./lib/data", () => ({
  useDashboardData: () => dashboardState,
}));
vi.mock("./lib/theme", () => ({ useTheme: () => ({ mode: "light", toggle: vi.fn() }) }));
vi.mock("./lib/useHashRoute", () => ({
  ROUTES: [
    { id: "overview", label: "Overview" },
    { id: "methodology", label: "Methodology" },
    { id: "lab", label: "Strategy Lab" },
  ],
  useHashRoute: () => ({ route: routeState.route, navigate: vi.fn() }),
}));
vi.mock("./components/layout/TopBar", () => ({ TopBar: () => <header>Navigation</header> }));
vi.mock("./components/layout/PublicationStatusBanner", () => ({ PublicationStatusBanner: () => null }));
vi.mock("./components/Footer", () => ({ Footer: ({ disclaimer }: { disclaimer: string }) => <footer>{disclaimer}</footer> }));
vi.mock("./components/ErrorBoundary", () => ({ ErrorBoundary: ({ children }: { children: React.ReactNode }) => children }));
vi.mock("./components/views/OverviewView", () => ({
  OverviewView: () => <h1 id="overview-page-title" tabIndex={-1}>Overview heading</h1>,
}));
vi.mock("./components/views/MethodologyView", () => ({
  default: () => <h1 id="methodology-page-title" tabIndex={-1}>Methodology heading</h1>,
}));
vi.mock("./components/views/StrategyLabView", () => ({
  default: () => <h1 id="lab-page-title" tabIndex={-1}>Strategy heading</h1>,
}));

import App from "./App";

describe("application landmarks and route focus", () => {
  it("provides a skip link targeting one labeled semantic main region", async () => {
    routeState.route = "overview";
    window.location.hash = "#/overview";
    render(<App />);

    const skip = screen.getByRole("link", { name: "Skip to main content" });
    expect(skip).toHaveAttribute("href", "#main-content");
    const main = await screen.findByRole("main", { name: "Overview heading" });
    expect(main).toHaveClass("min-h-screen");
    fireEvent.click(skip);
    expect(main).toHaveFocus();
    expect(window.location.hash).toBe("#/overview");
  });

  it("focuses the labeled route heading after navigation", async () => {
    routeState.route = "overview";
    const { rerender } = render(<App />);
    await screen.findByText("Overview heading");

    routeState.route = "methodology";
    rerender(<App />);
    const heading = await screen.findByText("Methodology heading");
    await waitFor(() => expect(heading).toHaveFocus());
  });

  it.each([
    ["null trading signal", { forecast: {}, story: {}, signal: null }],
    ["missing trading signal", { forecast: {}, story: {} }],
  ])("renders a controlled missing-data state for a %s", async (_label, malformed) => {
    dashboardState.data = malformed;
    dashboardState.error = null;
    routeState.route = "overview";

    render(<App />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load the forecast");
    expect(screen.getByText("Published trading signal is missing or invalid.")).toBeInTheDocument();
    expect(screen.getByText("Paper-trading research only. No live orders are ever placed.")).toBeInTheDocument();
  });
});
