import { act, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PublicationProvider, type PublicationManifest } from "../../lib/publication";
import { PublicationLoaded } from "../../test/PublicationLoaded";
import type { City } from "../../lib/data";
import { CityDetail } from "./CityDetail";

const city: City = {
  slug: "sea",
  name: "Seattle",
  series_ticker: "KXHIGHTSEA",
  station_id: "KSEA",
  settlement_today: "2026-07-09",
  forecasts: [
    {
      target_date: "2026-07-09",
      target_status: "settlement_day",
      predicted_high_f: 71,
      fetched_at: "2026-07-09T11:59:00Z",
    },
  ],
  latest_settlement: { local_date: "2026-07-08", high_f: 70 },
  books: {
    live: { open_positions: 2, open_exposure: 20, settled_orders: 4, settled_pnl: 3 },
    research: { open_positions: 1, open_exposure: 10, settled_orders: 5, settled_pnl: 2 },
    decisions_24h: 12,
    approved_24h: 2,
  },
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

describe("CityDetail publication truthfulness", () => {
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

  async function renderDetail(generatedAt: string) {
    fetchMock.mockResolvedValue(ok(publication(generatedAt)));
    render(
      <PublicationProvider>
        <PublicationLoaded artifacts={["trading_signal.json", "cities_data.json"]} />
        <CityDetail city={city} />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));
  }

  it("withholds stale open-position and exposure values while retaining settled history", async () => {
    await renderDetail("2026-07-07T12:00:00Z");

    expect(within(screen.getByRole("row", { name: /Open positions/i })).getAllByText("Unavailable")).toHaveLength(2);
    expect(within(screen.getByRole("row", { name: /Open exposure/i })).getAllByText("Unavailable")).toHaveLength(2);
    expect(screen.getByRole("row", { name: /Settled orders/i })).toHaveTextContent("4");
  });

  it("shows current open-position and exposure values while publication is fresh", async () => {
    await renderDetail("2026-07-09T11:59:00Z");

    expect(screen.getByRole("row", { name: /Open positions/i })).toHaveTextContent("2");
    expect(screen.getByRole("row", { name: /Open exposure/i })).toHaveTextContent("$20.00");
  });
});
