import { useEffect, useState } from "react";
import { usePublication } from "./publication";

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
  /** Independent held-out forecast days behind lstm_sigma (not hourly rows). */
  lstm_sigma_days?: number;
  lstm_sigma_se?: number;
  n_years: number;
  years: number[];
  n_days_observed?: number;
  window_days: number;
  table?: Record<string, ClimatologyDay>;
}

export interface MonthlyTemp {
  month: number; // 1-12
  mean: number;
  min: number;
  max: number;
}
/** What a single histogram count represents. The publisher stamps this
    explicitly; an ABSENT marker means the legacy payload, which binned raw
    hourly readings (~24 per observed day) and must never be presented as
    daily highs. */
export type HistogramBasis = "daily_max" | "hourly";
export interface WeatherStory {
  temperature_histogram?: { labels: number[]; counts: number[]; basis?: string };
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
export type TargetStatus = "settlement_day" | "upcoming" | "past";
export interface Target {
  target_date: string;
  target_status?: TargetStatus;
  market_data_at?: string | null;
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
  generated_at?: string;
  market_data_at?: string | null;
  mode: string;
  disclaimer: string;
  live_orders_enabled: boolean;
  summary: { approved_signal_count: number; best_signal?: Decision };
  targets: Target[];
  calibration?: {
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
    warnings?: string[];
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
  target_status?: TargetStatus;
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
  settlement_today?: string;
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

async function getJSON<T>(name: string, version: string | null, signal: AbortSignal): Promise<T> {
  const suffix = version ? `?v=${encodeURIComponent(version)}` : "";
  const res = await fetch(`${BASE}${name}${suffix}`, { cache: "no-store", signal });
  if (!res.ok) throw new Error(`${name}: HTTP ${res.status}`);
  return (await res.json()) as T;
}

interface ArtifactRequest {
  promise: Promise<unknown>;
  controller: AbortController;
  subscribers: number;
  settled: boolean;
}

/** One request per published artifact VERSION. `cache: "no-store"` stops the
    browser from collapsing identical URLs, so three simultaneously mounted
    Methodology widgets each opened their own cities_data.json fetch and could
    briefly render disagreeing city counts. */
const artifactRequests = new Map<string, ArtifactRequest>();

const artifactKey = (name: string, version: string | null) =>
  version ? `${name}?v=${encodeURIComponent(version)}` : name;

/** Subscribe to a published artifact. Returns the shared promise plus a release
    for the effect cleanup: the last subscriber leaving before the body arrives
    aborts the request outright rather than discarding a response that is still
    on the wire. */
function acquireArtifact<T>(
  name: string,
  version: string | null,
): { promise: Promise<T>; release: () => void } {
  const key = artifactKey(name, version);
  let request = artifactRequests.get(key);
  if (!request) {
    // Exactly one version of an artifact is ever current, so opening a new key
    // retires every older one — a long-lived tab can neither accumulate bodies
    // nor be handed the previous publish cycle's copy after the manifest rotates.
    for (const superseded of Array.from(artifactRequests.keys())) {
      if (superseded === name || superseded.startsWith(`${name}?v=`)) {
        artifactRequests.delete(superseded);
      }
    }
    const controller = new AbortController();
    const created: ArtifactRequest = {
      promise: getJSON<T>(name, version, controller.signal),
      controller,
      subscribers: 0,
      settled: false,
    };
    created.promise.then(
      () => {
        created.settled = true;
      },
      () => {
        // Never cache a rejection: a transient failure has to stay retryable.
        if (artifactRequests.get(key) === created) artifactRequests.delete(key);
      },
    );
    artifactRequests.set(key, created);
    request = created;
  }
  const active = request;
  active.subscribers += 1;
  let released = false;
  return {
    promise: active.promise as Promise<T>,
    release: () => {
      if (released) return;
      released = true;
      active.subscribers -= 1;
      if (active.subscribers <= 0 && !active.settled) {
        active.controller.abort();
        if (artifactRequests.get(key) === active) artifactRequests.delete(key);
      }
    },
  };
}

export function useDashboardData() {
  const { acknowledgeArtifactLoaded, manifestSettled, versionForArtifact } = usePublication();
  const forecastVersion = versionForArtifact("forecast_data.json");
  const storyVersion = versionForArtifact("weather_story_data.json");
  const signalVersion = versionForArtifact("trading_signal.json");
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // A bare URL (no ?v=) is exactly what the CDN may answer from a ten-minute
    // edge-cache entry, so nothing may be fetched until the manifest has had its
    // say. A manifest FAILURE settles too — the page then loads unversioned
    // rather than rendering nothing at all.
    if (!manifestSettled) return;
    let alive = true;
    setError(null);
    const requests = [
      acquireArtifact<ForecastData>("forecast_data.json", forecastVersion),
      acquireArtifact<WeatherStory>("weather_story_data.json", storyVersion),
      acquireArtifact<TradingSignal>("trading_signal.json", signalVersion),
    ] as const;
    Promise.all([requests[0].promise, requests[1].promise, requests[2].promise])
      .then(([forecast, story, signal]) => {
        if (!alive) return;
        setData({ forecast, story, signal });
        acknowledgeArtifactLoaded("forecast_data.json", forecastVersion);
        acknowledgeArtifactLoaded("weather_story_data.json", storyVersion);
        acknowledgeArtifactLoaded("trading_signal.json", signalVersion);
      })
      .catch((e: unknown) => {
        if (alive) setError(String(e));
      });
    return () => {
      alive = false;
      for (const request of requests) request.release();
    };
  }, [acknowledgeArtifactLoaded, forecastVersion, manifestSettled, signalVersion, storyVersion]);

  return { data, error };
}

/** Generic single-resource loader (used by the lazy Methodology / Strategy views). */
export function useResource<T>(name: string) {
  const { acknowledgeArtifactLoaded, manifestSettled, versionForArtifact } = usePublication();
  const version = versionForArtifact(name);
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!manifestSettled) return;
    let alive = true;
    setError(null);
    const request = acquireArtifact<T>(name, version);
    request.promise
      .then((d) => {
        if (!alive) return;
        setData(d);
        acknowledgeArtifactLoaded(name, version);
      })
      .catch((e: unknown) => {
        if (alive) setError(String(e));
      });
    return () => {
      alive = false;
      request.release();
    };
  }, [acknowledgeArtifactLoaded, manifestSettled, name, version]);
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

/* ---- per-city helpers (shared by the coverage grid and the city drill-down) ---- */

/** "2026-07-06" → "Jul 6" (date-only, timezone-safe via UTC). */
export function shortDateUTC(iso: string | undefined | null): string {
  if (!iso) return "—";
  const t = Date.parse(`${iso}T00:00:00Z`);
  if (Number.isNaN(t)) return iso;
  return new Date(t).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

/** The forecast a city leads with: backend settlement day first, then the
    earliest target after the latest settlement, then the first published row.
    The browser clock is not authoritative for city settlement days. */
export function cityNextForecast(city: City): CityForecast | null {
  const sorted = [...(city.forecasts ?? [])]
    .filter((f) => typeof f?.predicted_high_f === "number" && !!f?.target_date)
    .sort((a, b) => a.target_date.localeCompare(b.target_date));
  if (!sorted.length) return null;
  const hasPublishedStatuses = sorted.some((forecast) => forecast.target_status != null);
  if (hasPublishedStatuses) {
    return (
      sorted.find((forecast) => forecast.target_status === "settlement_day") ??
      sorted.find((forecast) => forecast.target_status === "upcoming") ??
      null
    );
  }
  if (city.settlement_today) {
    const current = sorted.find((f) => f.target_date >= city.settlement_today!);
    if (current) return current;
  }
  const settledDate = city.latest_settlement?.local_date;
  if (settledDate) {
    const next = sorted.find((f) => f.target_date > settledDate);
    if (next) return next;
  }
  return sorted[0];
}

/** Status-aware display order for current market targets. Legacy artifacts that
    predate target_status retain their published ordering. */
export function selectCurrentTargets(targets: Target[]): Target[] {
  const hasKnownStatus = (target: Target) =>
    target.target_status === "settlement_day" ||
    target.target_status === "upcoming" ||
    target.target_status === "past";
  const statusAware = targets.some(hasKnownStatus);
  if (!statusAware) return targets;

  const byDate = (a: Target, b: Target) => a.target_date.localeCompare(b.target_date);
  const settlementDay = targets.filter((target) => target.target_status === "settlement_day").sort(byDate);
  const upcoming = targets.filter((target) => target.target_status === "upcoming").sort(byDate);
  const legacy = targets.filter((target) => !hasKnownStatus(target));
  return [...settlementDay, ...upcoming, ...legacy];
}

const FRESH_GREEN_HOURS = 2;
const FRESH_AMBER_HOURS = 12;
export interface Freshness {
  tone: "success" | "warning" | "danger";
  label: string;
  ageHours: number | null;
}
/** How recently a city's forecasts were issued → a tone + human label. */
export function cityFreshness(forecasts: CityForecast[] | undefined): Freshness {
  let newest: number | null = null;
  for (const f of forecasts ?? []) {
    const t = Date.parse(f?.fetched_at ?? "");
    if (!Number.isNaN(t) && (newest == null || t > newest)) newest = t;
  }
  if (newest == null) return { tone: "danger", label: "No forecast issue recorded", ageHours: null };
  const hrs = Math.max(0, (Date.now() - newest) / 3_600_000);
  if (hrs < FRESH_GREEN_HOURS)
    return { tone: "success", label: `Forecast issued ${Math.max(1, Math.round(hrs * 60))}m ago`, ageHours: hrs };
  if (hrs < FRESH_AMBER_HOURS) return { tone: "warning", label: `Forecast issued ${Math.round(hrs)}h ago`, ageHours: hrs };
  return { tone: "danger", label: `Stale — forecast issued ${Math.round(hrs)}h ago`, ageHours: hrs };
}

/* ---- derived helpers ---- */

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Climatology table key ("03-14") → "Mar 14". Those keys carry no year. */
export function monthDayLabel(key: string): string {
  const [mm, dd] = key.split("-");
  const month = MONTHS[Number(mm) - 1];
  if (!month || !dd) return key;
  return `${month} ${Number(dd)}`;
}

// Full-year climatology series (one point per day-of-year), evenly sampled for a
// clean seasonal band chart.
export function climatologySeries(forecast: ForecastData, step = 3) {
  const table = forecast.table ?? {};
  const keys = Object.keys(table).sort();
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
  // Label the FIRST sampled day of each month. Keying the label off day-of-month
  // 15 only coincided with the sampling stride for five of the twelve months, so
  // the axis silently lost the other seven.
  let labelledMonth = "";
  keys.forEach((k, i) => {
    if (i % step !== 0) return;
    const d = table[k];
    const [mm] = k.split("-");
    const startsMonth = mm !== labelledMonth;
    if (startsMonth) labelledMonth = mm;
    out.push({
      key: k,
      label: startsMonth ? MONTHS[Number(mm) - 1] : "",
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

/** How the published histogram was binned — see {@link HistogramBasis}. An
    unmarked payload is legacy hourly data, never daily maxima. */
export function histogramBasis(story: WeatherStory): HistogramBasis {
  return story.temperature_histogram?.basis === "daily_max" ? "daily_max" : "hourly";
}

/** Bin EDGES reconstructed from published bin centres: an interior edge is the
    midpoint of two adjacent centres, and the two outer edges extrapolate the
    neighbouring bin width. Centres are published rounded to a tenth (36.2 for a
    true 36.25), so rounding them to whole degrees turned an even 2.5° grid into
    an uneven 36 / 39 / 41 / 44 tick sequence; the edges land on the grid. */
function histogramEdges(labels: number[]): number[] {
  if (labels.length === 0) return [];
  const inner: number[] = [];
  for (let i = 1; i < labels.length; i++) inner.push(round1((labels[i - 1] + labels[i]) / 2));
  if (inner.length === 0) return [round1(labels[0] - 0.5), round1(labels[0] + 0.5)];
  const lead = inner.length > 1 ? inner[1] - inner[0] : (inner[0] - labels[0]) * 2;
  const trail =
    inner.length > 1 ? inner[inner.length - 1] - inner[inner.length - 2] : lead;
  return [round1(inner[0] - lead), ...inner, round1(inner[inner.length - 1] + trail)];
}

export function histogramSeries(story: WeatherStory) {
  const { labels = [], counts = [] } = story.temperature_histogram ?? {};
  const edges = histogramEdges(labels);
  return labels.map((t, i) => ({
    temp: round1(t), // published bin centre — drives the thermal hue only
    lo: edges[i],
    hi: edges[i + 1],
    count: counts[i] ?? 0,
  }));
}

export function calibrationSeries(signal: TradingSignal) {
  return (signal.calibration?.buckets ?? []).map((b) => ({
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
    // --accent is the brand yellow, tuned for the dark canvas and decoration;
    // this paints a value in a 12px table cell, so it takes the text-legible
    // step (aliased straight back to --accent under .dark).
    : score >= 40 ? "var(--accent-text)"
    : score >= 20 ? "var(--color-warning)"
    : "var(--color-muted)";

/** Skill percent → KPI.Progress status enum (drives the bar hue by magnitude). */
export const skillStatus = (pctVal: number): "success" | "warning" | "danger" =>
  pctVal >= 50 ? "success" : pctVal >= 25 ? "warning" : "danger";

/* Ramp endpoints as CSS tokens. The ramp paints values, not just decoration,
   and its warm stops used to fall under 3:1 on a light canvas (a 78° card
   measured 2.94:1, a 77° card 2.34:1). index.css now carries the light-canvas
   ramp on these base tokens and restores the brighter stops under `.dark`, so
   reading them here is enough — one definition serves the temperature text,
   the histogram fills and the decorative glow. */
export const TEMP_RAMP_COLD = "var(--temp-cold)";
export const TEMP_RAMP_WARM = "var(--temp-warm)";
export const TEMP_RAMP_HOT = "var(--temp-hot)";

// Tight cool→warm→hot stops (no green). Interpolated in oklab so the midpoint is
// a muted neutral rather than a rainbow sweep through green/cyan.
const TEMP_STOPS: [number, string][] = [
  [48, TEMP_RAMP_COLD],
  [68, TEMP_RAMP_WARM],
  [86, TEMP_RAMP_HOT],
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

const CLOSED_BRACKET = /^(-?\d+(?:\.\d+)?)°?\s*to\s*(-?\d+(?:\.\d+)?)°?$/i;
const OR_BELOW_BRACKET = /^(-?\d+(?:\.\d+)?)°?\s*or\s*below$/i;
const OR_ABOVE_BRACKET = /^(-?\d+(?:\.\d+)?)°?\s*or\s*above$/i;

/** Kalshi bracket titles arrive in three shapes and the two OPEN-ENDED ones hold
    almost all of the settlement-day probability mass, so they cannot be treated
    as a malformed closed range: "76° to 77°" → "76–77°", "75° or below" → "≤75°",
    "84° or above" → "≥84°". Anything unrecognised is passed through untouched
    rather than mangled. Every form keeps its degree unit. */
export function binTickLabel(raw: string): string {
  const label = raw.trim();
  const closed = CLOSED_BRACKET.exec(label);
  if (closed) return `${closed[1]}–${closed[2]}°`;
  const below = OR_BELOW_BRACKET.exec(label);
  if (below) return `≤${below[1]}°`;
  const above = OR_ABOVE_BRACKET.exec(label);
  if (above) return `≥${above[1]}°`;
  return label;
}

// The edge engine's core view: model probability vs market-implied probability
// per market bin, for a given target.
export function marketModelSeries(target: Target | undefined) {
  const dist = target?.market_consensus?.distribution ?? [];
  return dist.map((b) => ({
    label: binTickLabel(b.label),
    // The market's own bracket title, carried through for the tooltip so the
    // reader sees the contract wording rather than an abbreviated tick.
    rawLabel: b.label,
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
  const cohorts = signal.calibration?.cohorts ?? [];
  return cohorts.map((c) => ({
    name: COHORT_LABELS[c.name] ?? c.name,
    skill: Math.round(c.ranked_probability_skill * 100),
    topBin: Math.round(c.top_bin_accuracy * 100),
    winProb: Math.round(c.avg_winning_probability * 100),
    count: c.count,
  }));
}

export const MONTH_LABELS = MONTHS;
