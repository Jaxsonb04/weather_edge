import { Skeleton } from "@heroui/react/skeleton";
import {
  cityFreshness,
  cityNextForecast,
  predictedHigh,
  round1,
  shortDateUTC,
  tempColor,
  type City,
  type CitiesData,
  type CityForecast,
  type Target,
} from "../../lib/data";
import { usePublication } from "../../lib/publication";

const GRID = "grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5";
const DOT_TONE: Record<string, string> = {
  success: "bg-success",
  warning: "bg-warning",
  danger: "bg-danger",
};

/** The flagship market signal republishes San Francisco's settlement-day high
    after folding in the day's observed high so far, so it can differ by degrees
    from the plain EMOS issue the coverage artifact carries for the same date.
    Both overview surfaces reconcile against this, so one page never shows two
    different highs for one city. Bracket-level signal data is SFO-only. */
export interface IntradayLock {
  slug: string;
  targetDate: string;
  /** The intraday-updated high (°F) — the number the hero dial shows. */
  highF: number;
}

/** Below this the two artifacts agree at any precision the page renders. */
const LOCK_EPSILON_F = 0.05;

/** Build the flagship reconciliation from the current signal targets, or null
    when no published target carries an intraday update. */
export function flagshipIntradayLock(targets: Target[]): IntradayLock | null {
  for (const target of targets) {
    if (!target.intraday) continue;
    const high = predictedHigh(target);
    if (high == null) continue;
    return { slug: "sfo", targetDate: target.target_date, highF: high };
  }
  return null;
}

/** The high to display for one city forecast. `baselineF` is set only when the
    intraday update actually moved the number, so callers label the adjustment
    instead of silently swapping one published figure for the other. */
export function lockedHigh(
  slug: string | undefined,
  forecast: CityForecast,
  lock: IntradayLock | null | undefined,
): { highF: number; baselineF: number | null } {
  if (!lock || lock.slug !== slug || lock.targetDate !== forecast.target_date) {
    return { highF: forecast.predicted_high_f, baselineF: null };
  }
  const moved = Math.abs(lock.highF - forecast.predicted_high_f) >= LOCK_EPSILON_F;
  return { highF: lock.highF, baselineF: moved ? forecast.predicted_high_f : null };
}

function CityCard({
  city,
  isActive,
  onSelect,
  currentStateAvailable,
  intradayLock,
}: {
  city: City;
  isActive: boolean;
  onSelect: () => void;
  currentStateAvailable: boolean;
  intradayLock: IntradayLock | null;
}) {
  const fc = cityNextForecast(city);
  const display = fc ? lockedHigh(city.slug ?? city.series_ticker, fc, intradayLock) : null;
  const preLockHighF = display?.baselineF ?? null;
  const fresh = cityFreshness(city.forecasts);
  const settled = city.latest_settlement;
  const openPositions =
    (city.books?.live?.open_positions ?? 0) + (city.books?.research?.open_positions ?? 0);
  const scans = city.books?.decisions_24h ?? 0;

  return (
    // No aria-label here: role=button computes its name from content, and an
    // aria-label would REPLACE every number on the card in the accessibility
    // tree — a screen reader would hear fifteen identical "show city detail"
    // buttons and none of the forecast, settlement or book figures.
    <button
      type="button"
      aria-pressed={isActive}
      onClick={onSelect}
      className={`flex h-full w-full flex-col gap-2.5 rounded-2xl p-3.5 text-left ring-1 transition-[box-shadow,transform,background-color] duration-200 hover:-translate-y-0.5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--focus)] motion-reduce:transition-none motion-reduce:hover:translate-y-0 ${
        isActive
          ? "bg-accent-soft ring-2 ring-accent"
          : "bg-surface ring-border/70 hover:ring-border"
      }`}
    >
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="truncate font-display text-sm font-semibold text-foreground">{city.name ?? "—"}</p>
            <p className="font-mono text-[10px] uppercase tracking-wider text-muted">
              {city.station_id ?? "—"}
            </p>
          </div>
          <span
            role="img"
            aria-label={fresh.label}
            title={fresh.label}
            className={`mt-1 size-2 shrink-0 rounded-full ${DOT_TONE[fresh.tone] ?? "bg-muted"}`}
          />
        </div>

        <div>
          <div className="flex items-baseline gap-1.5">
            {fc && display ? (
              <>
                <span
                  className="tnum font-display text-3xl font-bold leading-none"
                  style={{ color: tempColor(display.highF) }}
                >
                  {Math.round(display.highF)}°
                </span>
                {/* Sigma belongs to the EMOS issue, so it moves to the line
                    below whenever the intraday update has replaced the high. */}
                {preLockHighF == null && typeof fc.sigma_f === "number" && (
                  <span className="tnum font-mono text-[11px] text-muted">±{round1(fc.sigma_f)}°</span>
                )}
              </>
            ) : (
              <span className="font-display text-3xl font-bold leading-none text-muted">—</span>
            )}
          </div>
          <p className="mt-1 font-mono text-[10px] uppercase tracking-wider text-muted">
            {fc ? `${shortDateUTC(fc.target_date)} · ${fc.n_models ?? "—"} models` : "no current forecast"}
          </p>
          {fc && preLockHighF != null && (
            <p className="mt-1 text-[10px] leading-snug text-muted">
              {`Intraday-updated · EMOS issue ${round1(preLockHighF)}°${
                typeof fc.sigma_f === "number" ? ` ±${round1(fc.sigma_f)}°` : ""
              }`}
            </p>
          )}
        </div>

        <div className="mt-auto space-y-1 border-t border-border/50 pt-2 text-[11px] leading-snug text-muted">
          <p>
            {settled ? (
              <>
                Settled {shortDateUTC(settled.local_date)}:{" "}
                <span className="tnum font-medium text-foreground">{round1(settled.high_f)}°</span>
              </>
            ) : (
              "No settlement yet"
            )}
          </p>
          <p className="tnum">
            {!currentStateAvailable
              ? "Current book status unavailable"
              : openPositions > 0
              ? `${openPositions} open position${openPositions === 1 ? "" : "s"}`
              : `${scans.toLocaleString()} decision evaluations/24h`}
          </p>
        </div>

        {city.has_full_blend && (
          <span
            className={`self-start rounded px-1.5 py-0.5 font-mono text-[10px] font-medium text-[color:var(--accent-text)] ${
              isActive ? "bg-accent/15" : "bg-accent-soft"
            }`}
          >
            flagship · blend-capable
          </span>
        )}
    </button>
  );
}

function EmptyNote() {
  return (
    <p className="rounded-2xl border border-dashed border-border/70 px-4 py-8 text-center text-sm text-muted">
      Multi-city data not yet published — the fifteen-city coverage artifact will appear here on the
      next pipeline run.
    </p>
  );
}

/** A failed fetch is not a pre-publication state, so it must not borrow the
    "not yet published" wording — that would read a 404/5xx/CORS outage as
    routine progress. */
function ErrorNote({ detail }: { detail?: string | null }) {
  return (
    <div
      role="alert"
      className="rounded-2xl border border-dashed border-danger/40 px-4 py-8 text-center"
    >
      <p className="text-sm text-muted">
        Couldn't load city coverage — the fifteen-city artifact did not load in this session.
        Reload the page to try again.
      </p>
      {detail && <p className="mt-2 font-mono text-[11px] text-muted">{detail}</p>}
    </div>
  );
}

interface CityGridProps {
  data: CitiesData | null;
  error?: string | null;
  selected: string;
  onSelect: (slug: string) => void;
  /** Flagship reconciliation, so a card never contradicts the hero dial. */
  intradayLock?: IntradayLock | null;
}

/** The fifteen-city coverage grid, now the primary navigator: each card is a
    button that sets the active city; the selected card is clearly marked. Cards
    lead with the next calibrated high, with settlement + book activity quiet
    underneath. */
export function CityGrid({ data, error, selected, onSelect, intradayLock = null }: CityGridProps) {
  const { operational } = usePublication();
  const currentStateAvailable = operational.state === "fresh";
  if (!data) {
    if (error) return <ErrorNote detail={error} />;
    return (
      <div className={GRID} aria-hidden="true">
        {Array.from({ length: 15 }).map((_, i) => (
          <Skeleton key={i} className="h-44 rounded-2xl" />
        ))}
      </div>
    );
  }
  const cities = data.cities ?? [];
  if (!cities.length) return <EmptyNote />;
  return (
    <div className={GRID} role="group" aria-label="City coverage — select a city">
      {cities.map((c) => {
        const slug = c.slug ?? c.series_ticker;
        return (
          <CityCard
            key={slug}
            city={c}
            isActive={slug === selected}
            onSelect={() => onSelect(slug)}
            currentStateAvailable={currentStateAvailable}
            intradayLock={intradayLock}
          />
        );
      })}
    </div>
  );
}
