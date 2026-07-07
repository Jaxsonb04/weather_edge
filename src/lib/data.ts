import { useEffect, useState } from "react";

/* ---- shapes of the published artifacts (subset we render) ---- */

export interface ClimatologyDay {
  mean: number;
  std: number;
  p10: number;
  p90: number;
  record_high: number;
  record_low: number;
  n: number;
}
export interface ForecastData {
  lstm_sigma: number;
  lstm_bias: number;
  n_years: number;
  years: number[];
  n_days_observed: number;
  window_days: number;
  table: Record<string, ClimatologyDay>;
}

export interface MonthlyTemp {
  month: number; // 1-12
  mean: number;
  min: number;
  max: number;
}
export interface WeatherStory {
  temperature_histogram: { labels: number[]; counts: number[] };
  monthly_temperature?: Record<string, MonthlyTemp>;
}

export interface Decision {
  ticker: string;
  label: string;
  action: string;
  side: string;
  approved: boolean;
  decision: string;
  probability: number;
  probability_lcb: number;
  model_probability: number;
  market_probability: number;
  edge: number;
  edge_lcb: number;
  trade_quality_score: number;
  recommended_spend: number;
  reasons: string[];
}
export interface MarketBin {
  center_f: number;
  label: string;
  ticker: string;
  implied_probability: number;
  model_probability: number;
}
export interface MarketConsensus {
  available: boolean;
  distribution: MarketBin[];
  implied_high_f: number;
  model_high_f: number;
  model_minus_market_f: number;
  modal_bin_label: string;
  modal_probability: number;
  implied_stdev_f: number;
  overround: number;
}
export interface Intraday {
  is_complete: boolean;
  latest_temp_f: number;
  observed_high_f: number;
  observed_high_source: string;
  remaining_forecast_high_f: number;
  observation_count: number;
  latest_observed_at: string;
}
export interface Target {
  target_date: string;
  event_title?: string;
  market_available: boolean;
  best_decision: Decision;
  decisions: Decision[];
  forecast?: Record<string, number | string | null>;
  ensemble?: Record<string, number | null>;
  intraday?: Intraday;
  market_consensus?: MarketConsensus;
  warnings?: string[];
}
export interface CalibrationBucket {
  range: string;
  lower: number;
  upper: number;
  avg_probability: number;
  observed_frequency: number;
  count: number;
}
export interface Cohort {
  name: string;
  count: number;
  brier_score: number;
  ranked_probability_skill: number;
  top_bin_accuracy: number;
  avg_winning_probability: number;
}
export interface TradingSignal {
  mode: string;
  disclaimer: string;
  live_orders_enabled: boolean;
  summary: { approved_signal_count: number; best_signal?: Decision };
  targets: Target[];
  calibration: {
    brier_score: number;
    brier_skill: number;
    ranked_probability_score: number;
    ranked_probability_skill: number;
    top_bin_accuracy: number;
    log_loss: number;
    avg_entropy: number;
    n: number;
    buckets: CalibrationBucket[];
    cohorts?: Cohort[];
  };
}

export interface DashboardData {
  forecast: ForecastData;
  story: WeatherStory;
  signal: TradingSignal;
}

/* ---- multi-city artifact (cities_data.json) ---- */

export interface CityForecast {
  target_date: string;
  lead_days?: number;
  predicted_high_f: number;
  sigma_f?: number | null;
  n_models?: number | null;
  model_spread_f?: number | null;
  fetched_at?: string;
  method?: string;
}
export interface CitySettlement {
  local_date: string;
  high_f: number;
  fetched_at?: string;
  source?: string;
}
export interface CityBookSide {
  open_positions?: number;
  open_exposure?: number;
  settled_orders?: number;
  settled_pnl?: number;
}
export interface CityBooks {
  live?: CityBookSide;
  research?: CityBookSide;
  decisions_24h?: number;
  approved_24h?: number;
}
export interface City {
  slug: string;
  name: string;
  series_ticker: string;
  station_id?: string;
  settlement_source?: string;
  civil_tz?: string;
  has_full_blend?: boolean;
  forecasts?: CityForecast[];
  latest_settlement?: CitySettlement | null;
  books?: CityBooks | null;
}
export interface CitiesData {
  generated_at?: string;
  city_count?: number;
  cities_with_live_forecasts?: number;
  note?: string;
  cities?: City[];
}

const BASE = import.meta.env.BASE_URL ?? "./";

async function getJSON<T>(name: string): Promise<T> {
  const res = await fetch(`${BASE}${name}`);
  if (!res.ok) throw new Error(`${name}: HTTP ${res.status}`);
  return (await res.json()) as T;
}

export function useDashboardData() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    Promise.all([
      getJSON<ForecastData>("forecast_data.json"),
      getJSON<WeatherStory>("weather_story_data.json"),
      getJSON<TradingSignal>("trading_signal.json"),
    ])
      .then(([forecast, story, signal]) => {
        if (alive) setData({ forecast, story, signal });
      })
      .catch((e) => alive && setError(String(e)));
    return () => {
      alive = false;
    };
  }, []);

  return { data, error };
}

/** Generic single-resource loader (used by the lazy Methodology / Strategy views). */
export function useResource<T>(name: string) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    setData(null);
    setError(null);
    getJSON<T>(name)
      .then((d) => alive && setData(d))
      .catch((e) => alive && setError(String(e)));
    return () => {
      alive = false;
    };
  }, [name]);
  return { data, error };
}

/** Multi-city coverage artifact — tolerant of the file not being published yet
    (renders a quiet empty state instead of failing the page). */
export const useCitiesData = () => useResource<CitiesData>("cities_data.json");

/* ---- city ticker mapping ---- */

const CITY_TICKERS: { ticker: string; slug: string; name: string }[] = [
  { ticker: "KXHIGHMIA", slug: "mia", name: "Miami" },
  { ticker: "KXHIGHLAX", slug: "lax", name: "Los Angeles" },
  { ticker: "KXHIGHCHI", slug: "chi", name: "Chicago" },
  { ticker: "KXHIGHTATL", slug: "atl", name: "Atlanta" },
  { ticker: "KXHIGHNY", slug: "nyc", name: "New York" },
  { ticker: "KXHIGHTDAL", slug: "dal", name: "Dallas" },
  { ticker: "KXHIGHTSEA", slug: "sea", name: "Seattle" },
  { ticker: "KXHIGHPHIL", slug: "phl", name: "Philadelphia" },
  { ticker: "KXHIGHTPHX", slug: "phx", name: "Phoenix" },
  { ticker: "KXHIGHAUS", slug: "aus", name: "Austin" },
  { ticker: "KXHIGHTSFO", slug: "sfo", name: "San Francisco" },
  { ticker: "KXHIGHTHOU", slug: "hou", name: "Houston" },
  { ticker: "KXHIGHTOKC", slug: "okc", name: "Oklahoma City" },
  { ticker: "KXHIGHTBOS", slug: "bos", name: "Boston" },
  { ticker: "KXHIGHDEN", slug: "den", name: "Denver" },
];

/** Longest-prefix match of a full market ticker (e.g. "KXHIGHTSFO-26JUL07-B67")
    against the fifteen series tickers. Null when nothing matches. */
export function cityForTicker(ticker: string): { slug: string; name: string } | null {
  if (!ticker) return null;
  let best: (typeof CITY_TICKERS)[number] | null = null;
  for (const c of CITY_TICKERS) {
    if (ticker.startsWith(c.ticker) && (best == null || c.ticker.length > best.ticker.length)) {
      best = c;
    }
  }
  return best ? { slug: best.slug, name: best.name } : null;
}

/* ---- derived helpers ---- */

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Full-year climatology series (one point per day-of-year), evenly sampled for a
// clean seasonal band chart.
export function climatologySeries(forecast: ForecastData, step = 3) {
  const keys = Object.keys(forecast.table).sort();
  const out: {
    key: string;
    label: string;
    mean: number;
    p10: number;
    band: number; // p90 - p10, stacked on top of p10 for the band area
    p90: number;
    record_high: number;
    record_low: number;
  }[] = [];
  keys.forEach((k, i) => {
    if (i % step !== 0) return;
    const d = forecast.table[k];
    const [mm] = k.split("-");
    const day = k.split("-")[1];
    out.push({
      key: k,
      label: day === "15" ? MONTHS[Number(mm) - 1] : "",
      mean: round1(d.mean),
      p10: round1(d.p10),
      band: round1(d.p90 - d.p10),
      p90: round1(d.p90),
      record_high: round1(d.record_high),
      record_low: round1(d.record_low),
    });
  });
  return out;
}

export function histogramSeries(story: WeatherStory) {
  const { labels, counts } = story.temperature_histogram;
  return labels.map((t, i) => ({ temp: Math.round(t), count: counts[i] ?? 0 }));
}

export function calibrationSeries(signal: TradingSignal) {
  return signal.calibration.buckets.map((b) => ({
    p: Math.round(b.avg_probability * 100),
    predicted: Math.round(b.avg_probability * 100),
    observed: Math.round(b.observed_frequency * 100),
    ideal: Math.round(b.avg_probability * 100),
    count: b.count,
  }));
}

export const round1 = (n: number) => Math.round(n * 10) / 10;
export const f1 = (n: number | undefined | null) =>
  n == null || Number.isNaN(n) ? "—" : `${round1(n)}°`;
export const pct = (n: number | undefined | null, digits = 0) =>
  n == null || Number.isNaN(n) ? "—" : `${(n * 100).toFixed(digits)}%`;
// Percent with an explicit "+" on positives, so favorable ≠ neutral by color alone.
export const signedPct = (n: number | undefined | null, digits = 0) =>
  n == null || Number.isNaN(n) ? "—" : `${n > 0 ? "+" : ""}${(n * 100).toFixed(digits)}%`;

/** Safe numeric read from a free-form forecast/ensemble blob (no unchecked casts). */
export const num = (r: Record<string, unknown> | undefined | null, k: string): number | null =>
  typeof r?.[k] === "number" ? (r[k] as number) : null;

/** Quality 0–100 → a magnitude-encoding color (dual-encode alongside bar width). */
export const qualityColor = (score: number): string =>
  score >= 66 ? "var(--color-success)"
    : score >= 40 ? "var(--accent)"
    : score >= 20 ? "var(--color-warning)"
    : "var(--color-muted)";

/** Skill percent → KPI.Progress status enum (drives the bar hue by magnitude). */
export const skillStatus = (pctVal: number): "success" | "warning" | "danger" =>
  pctVal >= 50 ? "success" : pctVal >= 25 ? "warning" : "danger";

// Tight cool→warm→hot stops (no green). Interpolated in oklab so the midpoint is
// a muted neutral rather than a rainbow sweep through green/cyan.
const TEMP_STOPS: [number, string][] = [
  [48, "var(--temp-cold)"],
  [68, "var(--temp-warm)"],
  [86, "var(--temp-hot)"],
];
/** Temperature °F → a color along the cool→hot ramp (for temperature-valued marks). */
export function tempColor(tempF: number): string {
  if (tempF <= TEMP_STOPS[0][0]) return TEMP_STOPS[0][1];
  const last = TEMP_STOPS[TEMP_STOPS.length - 1];
  if (tempF >= last[0]) return last[1];
  for (let i = 0; i < TEMP_STOPS.length - 1; i++) {
    const [t0, c0] = TEMP_STOPS[i];
    const [t1, c1] = TEMP_STOPS[i + 1];
    if (tempF <= t1) {
      const fr = (tempF - t0) / (t1 - t0);
      return `color-mix(in oklab, ${c0} ${Math.round((1 - fr) * 100)}%, ${c1})`;
    }
  }
  return last[1];
}

export function targetLabel(iso: string): string {
  // Target dates are SFO settlement days, so "Today" must mean today in
  // San Francisco — not in the viewer's (or a build server's) timezone.
  const sfoToday = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Los_Angeles",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date()); // en-CA renders as YYYY-MM-DD
  const target0 = Date.parse(iso + "T00:00:00Z");
  const today0 = Date.parse(sfoToday + "T00:00:00Z");
  const diff = Math.round((target0 - today0) / 86400000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Tomorrow";
  if (diff === -1) return "Yesterday";
  return new Date(target0).toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

// Pull the predicted high from a target's forecast blob (several possible keys).
export function predictedHigh(t: Target | undefined): number | null {
  if (!t?.forecast) return null;
  const f = t.forecast;
  for (const key of ["predicted_high_f", "blended_high_f", "high_f", "predicted_high"]) {
    const v = f[key];
    if (typeof v === "number") return v;
  }
  return null;
}

// Seasonal monthly min/mean/max band — one point per calendar month.
export function monthlySeries(story: WeatherStory) {
  const m = story.monthly_temperature;
  if (!m) return [];
  return Object.values(m)
    .sort((a, b) => a.month - b.month)
    .map((d) => ({
      month: MONTHS[d.month - 1],
      min: round1(d.min),
      mean: round1(d.mean),
      max: round1(d.max),
      lo: round1(d.min),
      band: round1(d.max - d.min), // stacked on `lo` for the min–max area
    }));
}

// The edge engine's core view: model probability vs market-implied probability
// per market bin, for a given target.
export function marketModelSeries(target: Target | undefined) {
  const dist = target?.market_consensus?.distribution ?? [];
  return dist.map((b) => ({
    label: b.label.replace("° to ", "–").replace("°", ""),
    center: b.center_f,
    model: Math.round(b.model_probability * 100),
    market: Math.round(b.implied_probability * 100),
    ticker: b.ticker,
  }));
}

const COHORT_LABELS: Record<string, string> = {
  cold_below_60f: "Cold · <60°",
  normal_60_69f: "Normal · 60–69°",
  warm_70_79f: "Warm · 70–79°",
  hot_80f_plus: "Hot · 80°+",
};

// Per-temperature-regime skill — shows where the model is sharp vs humbled.
export function cohortSeries(signal: TradingSignal) {
  const cohorts = signal.calibration.cohorts ?? [];
  return cohorts.map((c) => ({
    name: COHORT_LABELS[c.name] ?? c.name,
    skill: Math.round(c.ranked_probability_skill * 100),
    topBin: Math.round(c.top_bin_accuracy * 100),
    winProb: Math.round(c.avg_winning_probability * 100),
    count: c.count,
  }));
}

export const MONTH_LABELS = MONTHS;
