import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PublicationProvider, type PublicationManifest } from "../../lib/publication";
import type { Target } from "../../lib/data";
import { ForecastDial } from "./ForecastDial";
import { PublicationLoaded } from "../../test/PublicationLoaded";

const target = {
  target_date: "2026-07-09",
  target_status: "settlement_day",
  market_available: true,
  forecast: { predicted_high_f: 68 },
  intraday: {
    is_complete: false,
    latest_temp_f: 64,
    observed_high_f: 66,
    observed_high_source: "station",
    remaining_forecast_high_f: 68,
    observation_count: 3,
    latest_observed_at: "2026-07-09T11:55:00Z",
  },
} as unknown as Target;

const targetWithConsensus = {
  ...target,
  market_consensus: {
    available: true,
    distribution: [],
    implied_high_f: 67,
    model_high_f: 68,
    model_minus_market_f: 1,
    modal_bin_label: "67 to 68",
    modal_probability: 0.3,
    implied_stdev_f: 2,
    overround: 1,
  },
} as unknown as Target;

const publication = (generatedAt: string): PublicationManifest => ({
  snapshot_id: "0123456789abcdef01234567",
  artifacts: {
    "trading_signal.json": { generated_at: generatedAt, sha256: "signal", status: "ready" },
    "cities_data.json": { generated_at: generatedAt, sha256: "cities", status: "ready" },
  },
});

const ok = (payload: unknown) =>
  ({ ok: true, status: 200, json: async () => payload }) as Response;

describe("ForecastDial publication truthfulness", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-09T12:00:00Z"));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    fetchMock.mockReset();
  });

  async function renderDial(generatedAt: string, selectedTarget: Target = target) {
    fetchMock.mockResolvedValue(ok(publication(generatedAt)));
    render(
      <PublicationProvider>
        <PublicationLoaded artifacts={["trading_signal.json", "cities_data.json"]} />
        <ForecastDial targets={[selectedTarget]} />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));
  }

  it("does not claim the market or observations are live when publication is stale", async () => {
    await renderDial("2026-07-07T12:00:00Z", targetWithConsensus);

    expect(screen.queryByText("Market live")).not.toBeInTheDocument();
    expect(screen.queryByText(/Live · 3 obs/)).not.toBeInTheDocument();
    expect(screen.queryByText("vs market")).not.toBeInTheDocument();
    expect(screen.queryByText(/market implies/i)).not.toBeInTheDocument();
    expect(screen.getByText("Current status unavailable")).toBeInTheDocument();
  });

  it("keeps current-state labels visible when publication is fresh", async () => {
    await renderDial("2026-07-09T11:59:00Z");

    expect(screen.getByText("Market live")).toBeInTheDocument();
    expect(screen.getByText(/Live · 3 obs/)).toBeInTheDocument();
  });

  it("does not wait for the below-fold city artifact before showing fresh signal status", async () => {
    fetchMock.mockResolvedValue(ok(publication("2026-07-09T11:59:00Z")));
    render(
      <PublicationProvider>
        <PublicationLoaded artifacts={["trading_signal.json"]} />
        <ForecastDial targets={[target]} />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));

    expect(screen.getByText("Market live")).toBeInTheDocument();
    expect(screen.queryByText("Current status unavailable")).not.toBeInTheDocument();
  });

});
