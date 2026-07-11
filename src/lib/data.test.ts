import { afterEach, describe, expect, it, vi } from "vitest";
import {
  calibrationSeries,
  cityNextForecast,
  climatologySeries,
  histogramSeries,
  cohortSeries,
  selectCurrentTargets,
  type City,
  type ForecastData,
  type Target,
  type TradingSignal,
  type WeatherStory,
} from "./data";

const target = (target_date: string, target_status?: string) =>
  ({ target_date, target_status }) as Target;

describe("cityNextForecast", () => {
  afterEach(() => vi.useRealTimers());

  it("uses the backend settlement day instead of the browser UTC date", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-09T01:00:00Z"));
    const city: City = {
      slug: "sfo",
      name: "San Francisco",
      series_ticker: "KXHIGHTSFO",
      settlement_today: "2026-07-08",
      forecasts: [
        { target_date: "2026-07-08", predicted_high_f: 67 },
        { target_date: "2026-07-09", predicted_high_f: 69 },
      ],
    };

    expect(cityNextForecast(city)?.target_date).toBe("2026-07-08");
  });

  it("falls back to the first published forecast without guessing the browser date", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-09T01:00:00Z"));
    const city: City = {
      slug: "sfo",
      name: "San Francisco",
      series_ticker: "KXHIGHTSFO",
      forecasts: [
        { target_date: "2026-07-07", predicted_high_f: 65 },
        { target_date: "2026-07-08", predicted_high_f: 67 },
      ],
    };

    expect(cityNextForecast(city)?.target_date).toBe("2026-07-07");
  });

  it("uses published target status and never falls back to an explicitly past row", () => {
    const city: City = {
      slug: "sfo",
      name: "San Francisco",
      series_ticker: "KXHIGHTSFO",
      forecasts: [
        { target_date: "2026-07-07", target_status: "past", predicted_high_f: 65 },
        { target_date: "2026-07-10", target_status: "upcoming", predicted_high_f: 69 },
        { target_date: "2026-07-09", target_status: "settlement_day", predicted_high_f: 68 },
      ],
    };

    expect(cityNextForecast(city)?.target_date).toBe("2026-07-09");
  });

  it("returns no current forecast when every status-aware row is past", () => {
    const city: City = {
      slug: "sfo",
      name: "San Francisco",
      series_ticker: "KXHIGHTSFO",
      forecasts: [
        { target_date: "2026-07-07", target_status: "past", predicted_high_f: 65 },
        { target_date: "2026-07-08", target_status: "past", predicted_high_f: 67 },
      ],
    };

    expect(cityNextForecast(city)).toBeNull();
  });
});

describe("selectCurrentTargets", () => {
  it("ignores a past first row and prioritizes settlement day before upcoming dates", () => {
    const targets = [
      target("2026-07-07", "past"),
      target("2026-07-10", "upcoming"),
      target("2026-07-09", "settlement_day"),
      target("2026-07-11", "upcoming"),
    ];

    expect(selectCurrentTargets(targets).map((row) => row.target_date)).toEqual([
      "2026-07-09",
      "2026-07-10",
      "2026-07-11",
    ]);
  });

  it("keeps legacy ordering when target status metadata is absent", () => {
    const targets = [target("2026-07-11"), target("2026-07-10")];

    expect(selectCurrentTargets(targets)).toEqual(targets);
  });

  it("keeps legacy rows while excluding a target explicitly marked past", () => {
    const targets = [target("2026-07-10"), target("2026-07-09", "past")];

    expect(selectCurrentTargets(targets)).toEqual([targets[0]]);
  });
});

describe("published artifact fallbacks", () => {
  it("returns an empty climatology series when the forecast table is missing", () => {
    expect(climatologySeries({} as ForecastData)).toEqual([]);
  });

  it("returns an empty histogram series when the histogram is missing", () => {
    expect(histogramSeries({} as WeatherStory)).toEqual([]);
  });

  it("returns an empty calibration series when calibration buckets are missing", () => {
    expect(calibrationSeries({} as TradingSignal)).toEqual([]);
    expect(cohortSeries({} as TradingSignal)).toEqual([]);
  });
});
