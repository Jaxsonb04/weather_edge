import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PublicationProvider, type PublicationManifest } from "../../lib/publication";
import { PublicationLoaded } from "../../test/PublicationLoaded";
import type { City } from "../../lib/data";
import { CityDetail } from "./CityDetail";
import type { IntradayLock } from "./CityGrid";

/** The runtime's own coverage tag on a settlement day, verbatim from production
    `cities_data.json` on 2026-09-07 — it already names the intraday fold-in. */
const RUNTIME_METHOD = "emos_wmean + intraday high-so-far update";

const cityWithIntraday = (slug: string, name: string): City => ({
  slug,
  name,
  series_ticker: `KXHIGHT${slug.toUpperCase()}`,
  station_id: `K${slug.toUpperCase()}`,
  settlement_today: "2026-07-09",
  forecasts: [
    {
      target_date: "2026-07-09",
      target_status: "settlement_day",
      predicted_high_f: 71,
      predicted_high_f_pre_intraday: 68.5,
      intraday_update: { applied: true },
      method: RUNTIME_METHOD,
      sigma_f: 2.9,
      n_models: 8,
      fetched_at: "2026-07-09T11:59:00Z",
    },
  ],
  latest_settlement: { local_date: "2026-07-08", high_f: 70 },
  books: { live: {}, research: {}, decisions_24h: 0, approved_24h: 0 },
});

const manifest: PublicationManifest = {
  snapshot_id: "0123456789abcdef01234567",
  artifacts: {
    "trading_signal.json": { generated_at: "2026-07-09T11:59:00Z", sha256: "signal", status: "ready" },
    "cities_data.json": { generated_at: "2026-07-09T11:59:00Z", sha256: "cities", status: "ready" },
  },
};

const ok = (payload: unknown) => ({ ok: true, status: 200, json: async () => payload }) as Response;

describe("CityDetail intraday provenance", () => {
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

  async function renderDetail(city: City, intradayLock: IntradayLock | null) {
    fetchMock.mockResolvedValue(ok(manifest));
    const view = render(
      <PublicationProvider>
        <PublicationLoaded artifacts={["trading_signal.json", "cities_data.json"]} />
        <CityDetail city={city} intradayLock={intradayLock} />
      </PublicationProvider>,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));
    return view;
  }

  it("credits the coverage artifact, not the SFO-only flagship signal, for a non-SFO city", async () => {
    const { container } = await renderDetail(cityWithIntraday("sea", "Seattle"), null);

    expect(container.textContent).toContain(
      "Intraday-updated: the coverage forecast folds the day's observed high so far into the published high, so it reads",
    );
    // The flagship market signal is San-Francisco-only; saying it moved Seattle's
    // number is exactly the provenance claim this page must not make.
    expect(container.textContent).not.toContain("flagship market signal");
  });

  it("credits the flagship signal only where the flagship lock supplied the high", async () => {
    const lock: IntradayLock = { slug: "sfo", targetDate: "2026-07-09", highF: 72.4 };
    const { container } = await renderDetail(cityWithIntraday("sfo", "San Francisco"), lock);

    expect(container.textContent).toContain(
      "Intraday-updated: the flagship market signal republishes this high after folding in the day's observed high so far, so it reads",
    );
  });

  it("renders the runtime method tag in plain English without doubling the intraday clause", async () => {
    const { container } = await renderDetail(cityWithIntraday("sea", "Seattle"), null);

    expect(container.textContent).toContain("EMOS weighted mean · updated with the day's observed high");
    expect(container.textContent).not.toContain("high-so-far update");
    expect(container.textContent).not.toContain("emos wmean");
  });
});
