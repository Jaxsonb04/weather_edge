import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@iconify/react/offline", () => ({ Icon: () => null }));

import { TopBar } from "../components/layout/TopBar";
import { TempUnitProvider, useTemperature } from "./units";

function Probe() {
  const t = useTemperature();
  return (
    <p>
      <span data-testid="abs">{t.temp(68)}</span>
      <span data-testid="abs-unit">{t.temp(50, 0, true)}</span>
      <span data-testid="delta">{t.diff(1.8)}</span>
      <span data-testid="missing">{t.temp(null)}</span>
    </p>
  );
}

const topBarProps = {
  mode: "dark" as const,
  onToggleTheme: vi.fn(),
  onOpenCommand: vi.fn(),
  menuOpen: false,
  onMenuOpenChange: vi.fn(),
  route: "overview" as const,
  repoUrl: "https://example.com/source",
  liveUrl: "https://example.com/live",
};

describe("temperature unit preference", () => {
  afterEach(() => localStorage.clear());

  it("defaults to Fahrenheit, the unit every artifact publishes", () => {
    render(<Probe />);
    expect(screen.getByTestId("abs")).toHaveTextContent("68°");
    expect(screen.getByTestId("abs-unit")).toHaveTextContent("50°F");
    expect(screen.getByTestId("delta")).toHaveTextContent("1.8°");
    expect(screen.getByTestId("missing")).toHaveTextContent("—");
  });

  it("converts readings with the offset and differences without it", () => {
    localStorage.setItem("weatheredge-temp-unit", "C");
    render(
      <TempUnitProvider>
        <Probe />
      </TempUnitProvider>,
    );
    expect(screen.getByTestId("abs")).toHaveTextContent("20°");
    expect(screen.getByTestId("abs-unit")).toHaveTextContent("10°C");
    expect(screen.getByTestId("delta")).toHaveTextContent("1°");
  });

  it("switches from the top bar and remembers the choice", () => {
    // jsdom has no Web Animations API; the real Segment moves its indicator on selection.
    Object.defineProperty(Element.prototype, "getAnimations", { configurable: true, value: () => [] });
    render(
      <TempUnitProvider>
        <TopBar {...topBarProps} />
        <Probe />
      </TempUnitProvider>,
    );
    const group = screen.getByRole("radiogroup", { name: "Temperature unit" });
    expect(group).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "Celsius" }));
    expect(screen.getByTestId("abs-unit")).toHaveTextContent("10°C");
    expect(localStorage.getItem("weatheredge-temp-unit")).toBe("C");

    fireEvent.click(screen.getByRole("radio", { name: "Fahrenheit" }));
    expect(screen.getByTestId("abs-unit")).toHaveTextContent("50°F");
    expect(localStorage.getItem("weatheredge-temp-unit")).toBe("F");
  });
});
