import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PublicationProvider, type PublicationManifest } from "../../lib/publication";
import { PublicationLoaded } from "../../test/PublicationLoaded";
import type { CitiesData } from "../../lib/data";
import { CityGrid } from "./CityGrid";

const data: CitiesData = {
  generated_at: "2026-07-09T11:59:00Z",
  cities: [
    {
      slug: "sfo",
      name: "San Francisco",
      series_ticker: "KXHIGHTSFO",
      station_id: "KSFO",
      settlement_today: "2026-07-09",
      forecasts: [
        {
          target_date: "2026-07-09",
          target_status: "settlement_day",
          predicted_high_f: 68,
          fetched_at: "2026-07-09T11:59:00Z",
        },
      ],
      books: {
        live: { open_positions: 2, resting_orders: 1, open_exposure: 20 },
        research: { open_positions: 1, open_exposure: 10 },
        decisions_24h: 12,
      },
    },
  ],
};

const publication = (generatedAt: string): PublicationManifest => ({
  snapshot_id: "0123456789abcdef01234567",
  artifacts: {
    "trading_signal.json": { generated_at: generatedAt, sha256: "signal", status: "ready" },
    "cities_data.json": { generated_at: generatedAt, sha256: "cities", status: "ready" },
  },
});

const ok = (payload: unknown) =>
  ({ ok: true, status: 200, json: async () => payload }) as Response;

describe("CityGrid publication truthfulness", () => {
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

  async function renderGrid(generatedAt: string) {
    fetchMock.mockResolvedValue(ok(publication(generatedAt)));
    render(
      <PublicationProvider>
        <PublicationLoaded artifacts={["trading_signal.json", "cities_data.json"]} />
        <CityGrid data={data} selected="sfo" onSelect={() => undefined} />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));
  }

  it("withholds stale open-position counts", async () => {
    await renderGrid("2026-07-07T12:00:00Z");

    expect(screen.queryByText(/open/)).not.toBeInTheDocument();
    expect(screen.getByText("Current book status unavailable")).toBeInTheDocument();
  });

  // Live and Research are economically separate paper accounts; one combined
  // "3 open positions" figure is exactly what this card must never print.
  it("reports each book separately and never sums the two accounts", async () => {
    await renderGrid("2026-07-09T11:59:00Z");

    expect(screen.getByText(/Live\s*2\s*open · 1 resting/)).toBeInTheDocument();
    expect(screen.getByText(/Research\s*1\s*open/)).toBeInTheDocument();
    expect(screen.queryByText(/3 open positions/)).not.toBeInTheDocument();
  });

  it("never labels a post-intraday high as the plain N-model forecast", async () => {
    const intraday: CitiesData = {
      ...data,
      cities: [
        {
          ...data.cities![0],
          forecasts: [
            {
              target_date: "2026-07-09",
              target_status: "settlement_day",
              predicted_high_f: 82.4,
              predicted_high_f_pre_intraday: 80.7,
              intraday_update: { applied: true },
              sigma_f: 2.9,
              n_models: 8,
              fetched_at: "2026-07-09T11:59:00Z",
            },
          ],
        },
      ],
    };
    fetchMock.mockResolvedValue(ok(publication("2026-07-09T11:59:00Z")));
    render(
      <PublicationProvider>
        <PublicationLoaded artifacts={["trading_signal.json", "cities_data.json"]} />
        <CityGrid data={intraday} selected="sfo" onSelect={() => undefined} />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));

    expect(screen.getByText("Jul 9 · 8-model EMOS + intraday")).toBeInTheDocument();
    expect(screen.getByText("Intraday-updated · EMOS issue 80.7° ±2.9°")).toBeInTheDocument();
  });
});
