import { afterEach, describe, expect, it, vi } from "vitest";
import {
  calibrationSeries,
  cityForTicker,
  cityFreshness,
  climatologySeries,
  cohortSeries,
  histogramSeries,
  marketModelSeries,
  monthlySeries,
  predictedHigh,
  targetLabel,
  type CityForecast,
  type ForecastData,
  type Target,
  type TradingSignal,
  type WeatherStory,
} from "./data";

describe("targetLabel settlement clock", () => {
  afterEach(() => vi.useRealTimers());

  it.each([
    ["2026-07-09T06:59:59Z", "Today"],
    ["2026-07-09T07:00:00Z", "Yesterday"],
  ])("labels the SFO target at %s as %s", (now, expected) => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(now));

    expect(targetLabel("2026-07-08")).toBe(expected);
  });
});

describe("city lookup and freshness", () => {
  afterEach(() => vi.useRealTimers());

  it.each([
    ["KXHIGHNY-26JUL11-B80", { slug: "nyc", name: "New York" }],
    ["KXHIGHTSFO-26JUL11-B68", { slug: "sfo", name: "San Francisco" }],
    ["KXHIGH-UNKNOWN", null],
    ["", null],
  ])("maps %s with the longest known series prefix", (ticker, expected) => {
    expect(cityForTicker(ticker)).toEqual(expected);
  });

  it.each([
    [1.99, "success"],
    [2, "warning"],
    [11.99, "warning"],
    [12, "danger"],
  ])("uses the documented freshness tone at %sh", (hours, tone) => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-09T12:00:00Z"));
    const forecasts = [{ fetched_at: new Date(Date.now() - hours * 3_600_000).toISOString() }] as CityForecast[];

    expect(cityFreshness(forecasts).tone).toBe(tone);
  });

  it("reports missing and future timestamps safely", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-09T12:00:00Z"));

    expect(cityFreshness(undefined)).toEqual({
      tone: "danger",
      label: "No forecast fetch recorded",
      ageHours: null,
    });
    expect(cityFreshness([{ fetched_at: "2026-07-09T13:00:00Z" }] as CityForecast[]).ageHours).toBe(0);
  });
});

describe("series helper fallbacks", () => {
  it.each([
    ["climatology", () => climatologySeries({} as ForecastData)],
    ["histogram", () => histogramSeries({} as WeatherStory)],
    ["monthly", () => monthlySeries({} as WeatherStory)],
    ["calibration", () => calibrationSeries({} as TradingSignal)],
    ["cohort", () => cohortSeries({} as TradingSignal)],
    ["market/model", () => marketModelSeries(undefined)],
  ])("returns an empty %s series when its optional fields are missing", (_name, build) => {
    expect(build()).toEqual([]);
  });

  it.each([
    [undefined, null],
    [{ forecast: {} } as Target, null],
    [{ forecast: { blended_high_f: 68.4 } } as unknown as Target, 68.4],
  ])("reads a predicted high without assuming optional fields", (target, expected) => {
    expect(predictedHigh(target)).toBe(expected);
  });
});
