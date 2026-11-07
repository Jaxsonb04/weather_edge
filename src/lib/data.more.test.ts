import { afterEach, describe, expect, it, vi } from "vitest";
import {
  calibrationSeries,
  cityForTicker,
  cityFreshness,
  climatologySeries,
  cohortSeries,
  describeMethod,
  fixtureCoverage,
  histogramSeries,
  marketModelSeries,
  monthlySeries,
  pct,
  plainReason,
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
    ["KXHIGHTLV-26SEP13-T98", { slug: "lv", name: "Las Vegas" }],
    ["KXHIGHTDC-26SEP13-T91", { slug: "dc", name: "Washington DC" }],
    ["KXHIGHTDAL-26SEP13-B80.5", { slug: "dal", name: "Dallas" }],
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
      label: "No forecast issue recorded",
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

describe("describeMethod", () => {
  // The runtime's own tag, verbatim from trading_signal.json on 2026-09-08.
  it("never surfaces the internal method tag", () => {
    expect(
      describeMethod(
        "emos_wmean (live NWP ensemble) [SFO operational fallback] + intraday high-so-far update",
      ),
    ).toBe(
      "EMOS weighted mean \u00b7 live NWP ensemble \u00b7 operational fallback \u00b7 updated with the day's observed high",
    );
  });

  it.each([
    ["emos_wmean", "EMOS weighted mean"],
    ["weighted blend + intraday high-so-far update", "weighted blend \u00b7 updated with the day's observed high"],
    [undefined, "Published forecast"],
    ["", "Published forecast"],
  ])("reads %s as plain English", (raw, expected) => {
    expect(describeMethod(raw)).toBe(expected);
  });
});

describe("plainReason", () => {
  it("drops the internal audit tag and check name", () => {
    expect(
      plainReason(
        "nonfinal-certainty gate: NO probability 0.995 >= 0.99 rests on a raw nonfinal station observation; the official integer report can settle a degree away (audit MD-01)",
      ),
    ).toBe(
      "Certainty check: NO probability 0.995 >= 0.99 rests on a raw not-yet-final station observation; the official integer report can settle a degree away",
    );
    expect(plainReason("edge -0.011 below min 0.012")).toBe("edge -0.011 below min 0.012");
  });
});

describe("pct", () => {
  it("rounds the decimal value, so 0.2945 reads 29.5% everywhere", () => {
    expect(pct(0.2945, 1)).toBe("29.5%");
    expect(pct(0.295, 0)).toBe("30%");
    expect(pct(-0.05, 0)).toBe("-5%");
    expect(pct(null)).toBe("—");
  });
});

describe("fixtureCoverage", () => {
  it.each([
    [undefined, null],
    [{ years: [] } as unknown as ForecastData, null],
    [{ years: [2020] } as unknown as ForecastData, "2020"],
    [{ years: [2016, 2026, 2021] } as unknown as ForecastData, "2016\u20132026"],
  ])("dates the committed study fixture from its own observation years", (forecast, expected) => {
    expect(fixtureCoverage(forecast)).toBe(expected);
  });
});
