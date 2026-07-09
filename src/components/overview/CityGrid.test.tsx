import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PublicationProvider, type PublicationManifest } from "../../lib/publication";
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
        live: { open_positions: 2, open_exposure: 20 },
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
        <CityGrid data={data} selected="sfo" onSelect={() => undefined} />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));
  }

  it("withholds stale open-position counts", async () => {
    await renderGrid("2026-07-07T12:00:00Z");

    expect(screen.queryByText("3 open positions")).not.toBeInTheDocument();
    expect(screen.getByText("Current book status unavailable")).toBeInTheDocument();
  });

  it("shows open-position counts while publication is fresh", async () => {
    await renderGrid("2026-07-09T11:59:00Z");

    expect(screen.getByText("3 open positions")).toBeInTheDocument();
  });
});
