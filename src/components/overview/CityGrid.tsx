import { Skeleton } from "@heroui/react/skeleton";
import {
  cityFreshness,
  cityNextForecast,
  round1,
  shortDateUTC,
  tempColor,
  type City,
  type CitiesData,
} from "../../lib/data";
import { usePublication } from "../../lib/publication";

const GRID = "grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5";
const DOT_TONE: Record<string, string> = {
  success: "bg-success",
  warning: "bg-warning",
  danger: "bg-danger",
};

function CityCard({
  city,
  isActive,
  onSelect,
  currentStateAvailable,
}: {
  city: City;
  isActive: boolean;
  onSelect: () => void;
  currentStateAvailable: boolean;
}) {
  const fc = cityNextForecast(city);
  const fresh = cityFreshness(city.forecasts);
  const settled = city.latest_settlement;
  const openPositions =
    (city.books?.live?.open_positions ?? 0) + (city.books?.research?.open_positions ?? 0);
  const scans = city.books?.decisions_24h ?? 0;

  return (
    <button
      type="button"
      aria-pressed={isActive}
      aria-label={`Show ${city.name ?? "city"} detail`}
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
            {fc ? (
              <>
                <span
                  className="tnum font-display text-3xl font-bold leading-none"
                  style={{ color: tempColor(fc.predicted_high_f) }}
                >
                  {Math.round(fc.predicted_high_f)}°
                </span>
                {typeof fc.sigma_f === "number" && (
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
              : `${scans.toLocaleString()} scans/24h`}
          </p>
        </div>

        {city.has_full_blend && (
          <span
            className={`self-start rounded px-1.5 py-0.5 font-mono text-[10px] font-medium text-[color:var(--accent-text)] ${
              isActive ? "bg-accent/15" : "bg-accent-soft"
            }`}
          >
            flagship · full blend
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

interface CityGridProps {
  data: CitiesData | null;
  error?: string | null;
  selected: string;
  onSelect: (slug: string) => void;
}

/** The fifteen-city coverage grid, now the primary navigator: each card is a
    button that sets the active city; the selected card is clearly marked. Cards
    lead with the next calibrated high, with settlement + book activity quiet
    underneath. */
export function CityGrid({ data, error, selected, onSelect }: CityGridProps) {
  const { operational } = usePublication();
  const currentStateAvailable = operational.state === "fresh";
  if (error && !data) return <EmptyNote />;
  if (!data) {
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
          />
        );
      })}
    </div>
  );
}
