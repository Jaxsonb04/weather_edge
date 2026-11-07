import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@iconify/react/offline", () => ({ Icon: () => null }));

import { TopBar } from "../components/layout/TopBar";
import { DecisionCard } from "../components/market/DecisionCard";
import { EdgeChart } from "../components/market/EdgeChart";
import { ForecastInputs } from "../components/market/ForecastInputs";
import type { Target } from "./data";
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

const marketTarget = {
  target_date: "2026-07-09",
  forecast: { predicted_high_f: 67.1, source_spread_f: 3.6, method: "emos_wmean (live NWP ensemble)" },
  ensemble: { member_count: 30, station_mean_high_f: 68, station_std_high_f: 1.8, station_bias_f: -0.9 },
  best_decision: { decision: "NO_TRADE", label: "67 to 68", ticker: "KX-T", model_probability: 0.3, market_probability: 0.3, edge: 0, trade_quality_score: 40, reasons: [] },
  market_consensus: {
    available: true,
    distribution: [{ label: "67° to 68°", center_f: 67.5, model_probability: 0.3, implied_probability: 0.3, ticker: "KX-T" }],
    implied_high_f: 68,
    model_high_f: 67.1,
    modal_bin_label: "67° to 68°",
  },
} as unknown as Target;

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

  it("renders 67.1°F as 19.5°C and a negative reading with its sign", () => {
    localStorage.setItem("weatheredge-temp-unit", "C");
    const Both = () => {
      const t = useTemperature();
      return <p data-testid="both">{t.temp(67.1, 0)} {t.temp(67.1)} {t.temp(14, 0)} {t.diff(-0.05)} {t.round(67.1, 0)}</p>;
    };
    render(<TempUnitProvider><Both /></TempUnitProvider>);
    // 67.1°F is exactly 19.5°C (computes as 19.4999…); -0.05°F diff is -0.03 and must read 0, not -0.
    expect(screen.getByTestId("both")).toHaveTextContent("20° 19.5° -10° 0° 67");
  });

  it("converts the market panel and marks Fahrenheit brackets in °C mode only", () => {
    localStorage.setItem("weatheredge-temp-unit", "C");
    const { unmount } = render(
      <TempUnitProvider>
        <ForecastInputs target={marketTarget} />
        <DecisionCard target={marketTarget} approvedCount={0} />
        <EdgeChart target={marketTarget} />
      </TempUnitProvider>,
    );
    expect(screen.getByText("19.5°")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "GFS ensemble detail: μ 20° · σ 1° · bias -0.5°" })).toBeInTheDocument();
    expect(screen.getByText(/published sources disagree by 2°/)).toBeInTheDocument();
    expect(screen.getByText(/model high 19.5° · market implies 20° · modal bin 67°F to 68°F/)).toBeInTheDocument();
    unmount();

    localStorage.setItem("weatheredge-temp-unit", "F");
    render(
      <TempUnitProvider>
        <ForecastInputs target={marketTarget} />
        <EdgeChart target={marketTarget} />
      </TempUnitProvider>,
    );
    expect(screen.getByText("67.1°")).toBeInTheDocument();
    expect(screen.getByText(/model high 67.1° · market implies 68° · modal bin 67° to 68°$/)).toBeInTheDocument();
  });
});
